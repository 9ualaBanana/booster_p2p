from datetime import datetime, timezone
from enum import Enum
from typing import List
from zoneinfo import ZoneInfo
from sqlalchemy import desc, or_
from config import TOKEN, API_KEY, ACCEPT_ORDER_TIMEOUT, TOP_LENGTH
from asyncio import run, wait_for
from decimal import Decimal, ROUND_HALF_EVEN
from typing import List
from config import TOKEN, API_KEY, ACCEPT_ORDER_TIMEOUT, TOP_LENGTH, ORDER_FEE
from creditcard import CreditCard
from database import OrderStatus, SessionFactory, User, Order
from fastapi import FastAPI, HTTPException, Header, Depends
import logging
from pydantic import BaseModel, field_validator
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler
from uvicorn import Config, Server
from order_controller import OrderController

# Add Order confirmation timeout.
# Define Pydantic models for FastAPI endpoint handlers.
# Add online status 4 working accounts.
# Implement username validator decorator.
# Host with certificates and shit.
# Ensure .env is reloadable.
# Prettyprint user account details on /start.
# Upgrade auth to JWT?

# Financial Data Integrity:

# Invalidate expired Orders after server restart according to `created_at`.
# Process Orders as ACID Transactions.
# Ensure concurrency issues are absent.

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

CHANGE_EXCHANGE_RATE, CHANGE_CARD_DETAILS, ORDER = range(3)

class HandlerNames(str, Enum):
    CHANGE_EXCHANGE_RATE = "change_exchange_rate"
    CHANGE_CARD_DETAILS = "change_card_details"
    BUY_USDT = "buy_usdt"
    ACCEPT_ORDER = "accept_order"
    DECLINE_ORDER = "decline_order"
    HANDLE_ORDER = "handle_order"
    COMPLETE_ORDER = "complete_order"
    CONFIRM_ORDER = "confirm_order"
    CALL_SUPPORT = "call_support"
    START_WORK = "start_work"
    STOP_WORK = "stop_work"

application = ApplicationBuilder().token(TOKEN).build()
app = FastAPI()

async def validate_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")

class OrderRequest(BaseModel):
    quantity: Decimal
    
    @field_validator('quantity')
    def validate_amount(cls, quantity):
        if quantity <= Decimal(0):
            raise ValueError("Amount must be positive.")
        return quantity.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)
    
class CompleteOrderRequest(BaseModel):
    order_id: str
    account_id: int

@app.post("/order", dependencies=[Depends(validate_api_key)])
async def order(order_request: OrderRequest):
    with SessionFactory() as session:
        # Registration process requires users to have @username.
        users = session.query(User).filter(
            User.is_working, 
            or_(~User.orders.any(), ~User.orders.any(Order.status != OrderStatus.COMPLETED)), 
            User.balance >= order_request.quantity
        ).order_by(User.exchange_rate).all()

        for user in users:
            order = Order(quantity=order_request.quantity, price=user.exchange_rate)
            user.orders.append(order)
            session.commit()

            notification = await application.bot.send_message(user.id,
                                                       f"Запрос на покупку {str(order_request.quantity.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)).rstrip('0').rstrip('.')} USDT\nБаланс: {user.formatted_balance} USDT\nПрибыль: {str((order_request.quantity * user.exchange_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)).rstrip('0').rstrip('.')} ₽",
                                                       reply_markup=InlineKeyboardMarkup([
                                                           [InlineKeyboardButton("Принять", callback_data=HandlerNames.ACCEPT_ORDER), InlineKeyboardButton("Отклонить", callback_data=HandlerNames.DECLINE_ORDER)]
                                                           ]))
            # Order Processing Session (should be persisted as well)
            # Add Order ID.
            order_controller = application.user_data[user.id].setdefault(Order.__name__, OrderController(notification, application.user_data[user.id]))

            try:
                await wait_for(order_controller.event.wait(), timeout=ACCEPT_ORDER_TIMEOUT)
                
                if order_controller := application.user_data[user.id].get(Order.__name__):
                    order_controller: OrderController
                    async with order_controller.lock:
                        session.refresh(user)

                        if order.status == OrderStatus.ACCEPTED:
                            user.balance -= order_request.quantity
                            user.frozen_balance += order_request.quantity
                            session.commit()

                            await order_controller.notification.edit_reply_markup(None)
                            await order_controller.start_confirmation_waiter()
                            
                            return {
                                "account": {
                                    "id": user.id,
                                    "card": user.card
                                },
                                "order": {
                                    "id": order.id,
                                    "price": order.price,
                                    "quantity": order.quantity
                                }
                            }
                        elif order.status == OrderStatus.DECLINED:
                            session.delete(order)
                            session.commit()

                            await order_controller.notification.delete()
                            await order_controller.cancel_confirmation_waiter()
                        else:
                            pass
                
            except TimeoutError:
                if order_controller := application.user_data[user.id].get(Order.__name__):
                    order_controller: OrderController
                    async with order_controller.lock:
                        session.refresh(user)

                        if order.status == OrderStatus.PENDING:
                            user.balance -= ORDER_FEE
                            session.delete(order)
                            session.commit()

                            await order_controller.notification.edit_text(f"Order {order.id} timed out. Processing fee ({ORDER_FEE} USDT) was deducted.", reply_markup=None)
                        else:
                            logging.debug("Handler won RC with timeout.")
            
            # Ends order processing session. Doesn't get executed if OrderStatus.ACCEPTED as it returns.
            # Shouldn't be popped until Order is finished processing, i.e. session is complete (COMPLETED | FAILED | DECLINED).
            application.user_data[user.id].pop(Order.__name__)
            # POST /order -> decline_order -> POST /order

        # Define the reason why order can't be completed.
        # It's either none of the users accepted it or there were no matching users who could complete the order.
        raise HTTPException(status_code=404, detail="Order can't be completed. None of the users accepted it.")
    
async def accept_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

    if order_controller := context.user_data.get(Order.__name__):
        order_controller: OrderController
        async with order_controller.lock:
            with SessionFactory() as session:
                order = session.query(Order).filter_by(user_id=update.effective_user.id).order_by(desc(Order.created_at)).first()
                if order.status == OrderStatus.PENDING:
                    order.status = OrderStatus.ACCEPTED
                    session.commit()
                    order_controller.event.set()

                    return
    
    logging.debug("Timeout won RC with handler.")

async def decline_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

    if order_controller := context.user_data.get(Order.__name__):
        order_controller: OrderController
        async with order_controller.lock:
            with SessionFactory() as session:
                order = session.query(Order).filter_by(user_id=update.effective_user.id).order_by(desc(Order.created_at)).first()
                if order.status == OrderStatus.PENDING:
                    order.status = OrderStatus.DECLINED
                    session.commit()
                    order_controller.event.set()

                    return
                
    logging.debug("Timeout won RC with handler.")
    
@app.patch("/order", dependencies=[Depends(validate_api_key)])
async def order(request: CompleteOrderRequest):
    # Assumed that it's the right order controller as it is the only one. Lacks ID validation.
    if order_controller := application.user_data[request.account_id].get(Order.__name__):
        order_controller: OrderController
        async with order_controller.lock:
            with SessionFactory() as session:
                order = session.query(Order).filter_by(id=request.order_id).one_or_none()
                if order and order.id == request.order_id:
                    if order.status == OrderStatus.ACCEPTED:
                        order.paid_at = datetime.now(timezone.utc)
                        session.commit()

                        await application.bot.send_message(order.user.id, f"Клиент оплатил {order.total_price} ₽", reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("Подтвердить", callback_data=HandlerNames.CONFIRM_ORDER), InlineKeyboardButton("Обратиться в тех. поддержку", callback_data=HandlerNames.CALL_SUPPORT)]
                            ]))
                    else:
                        raise HTTPException(status_code=409, detail="Order can't be completed. Conflicting status.")
                else:
                    raise HTTPException(status_code=404, detail="Order can't be completed. Order ID mismatch.")
        
async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    
    await update.effective_message.edit_text(f"{update.effective_message.text}\n\nВы уверены?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("Да", callback_data=HandlerNames.COMPLETE_ORDER), InlineKeyboardButton("Нет", callback_data=HandlerNames.HANDLE_ORDER)]
        ]))
    
async def handle_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

    if order_controller := context.user_data.get(Order.__name__):
        order_controller: OrderController
        async with order_controller.lock:
            with SessionFactory() as session:
                order = session.query(Order).filter_by(user_id=update.effective_user.id).order_by(desc(Order.created_at)).first()
                await update.effective_message.edit_text(f"Клиент оплатил {order.total_price} ₽", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Подтвердить", callback_data=HandlerNames.CONFIRM_ORDER), InlineKeyboardButton("Обратиться в тех. поддержку", callback_data=HandlerNames.CALL_SUPPORT)]
                    ]))
        
async def complete_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

    if order_controller := context.user_data.get(Order.__name__):
        order_controller: OrderController
        async with order_controller.lock:
            with SessionFactory() as session:
                order = session.query(Order).filter_by(user_id=update.effective_user.id).order_by(desc(Order.created_at)).first()
                if order.status == OrderStatus.ACCEPTED:
                    order.user.frozen_balance -= order.quantity
                    order.status = OrderStatus.COMPLETED
                    session.commit()
                    await update.effective_message.delete()
                    context.user_data.pop(Order.__name__)
                    # Send confirmation back to the client.

async def call_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    with SessionFactory() as session:
        order = session.query(Order).filter_by(user_id=update.effective_user.id).order_by(desc(Order.created_at)).first()
        await update.effective_message.reply_text(f"@techsupport\n\nОрдер ID: {order.id}\n{order.paid_at.astimezone(ZoneInfo("Europe/Moscow"))}")
    
async def order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.effective_message.reply_text("Сколько USDT вы хотите купить?")
    await update.effective_message.delete()
    
    return ORDER

async def receive_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = Decimal(update.message.text.strip()).quantize(Decimal('0.00'), rounding=ROUND_HALF_EVEN)
        if amount <= Decimal(0):
            await update.message.reply_text("Некорректная сумма USDT.")
            return
    except (ValueError, ArithmeticError):
        await update.message.reply_text("Некорректная сумма USDT.")
        return
        
    address = "123456789"  # Replace with actual address
    await update.message.reply_text(f"Адрес: {address}.")
    # Add buttons and handlers for cancelling lookup.

    # Move to actual handler that will validate deposit transaction.
    with SessionFactory() as session:
        user = session.query(User).filter_by(id=update.effective_user.id).one_or_none()
        
        if user:
            user.balance += amount
            session.commit()
            
            await update.message.reply_text(f"Баланс пополнен: {user.formatted_balance} USDT.")
        else:
            await update.message.reply_text("Аккаунт не найден.")
            # Must be impossible. Redirect to registration.

    return ConversationHandler.END

async def change_card_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.effective_message.reply_text("Предоставьте новые реквизиты:")
    await update.effective_message.delete()
    
    return CHANGE_CARD_DETAILS

async def receive_card_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card_number = ''.join(update.message.text.strip().split())
    if card_number.isdigit():
        card = CreditCard(card_number)
        if card.is_valid and not card.is_expired:
            with SessionFactory() as session:
                user: User | None = session.query(User).filter_by(id=update.effective_user.id).one_or_none()
                if user is None:
                    user = context.user_data.get('new_user', None)
                    if user is not None:
                        user.card = card.number
                        await update.message.reply_text(f"Реквизиты успешно изменены {card.number}.")
                        await update.effective_message.reply_text("Предоставьте новый курс:")
                        return CHANGE_EXCHANGE_RATE
                else:
                    user.card = card.number
                    session.commit()
                    await update.message.reply_text(f"Реквизиты успешно изменены {card.number}.")
                    await display_account(update, user, session)

                    return ConversationHandler.END
    
    await update.message.reply_text("Предоставленные реквезиты некорректны.")

async def change_exchange_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.effective_message.reply_text("Предоставьте новый курс:")
    await update.effective_message.delete()
    
    return CHANGE_EXCHANGE_RATE

async def receive_exchange_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_exchange_rate = Decimal(update.message.text.strip()).quantize(Decimal('0.000000'), rounding=ROUND_HALF_EVEN)
        if new_exchange_rate <= Decimal(0):
            await update.message.reply_text("Некорректный курс.")
            return
    except (ValueError, ArithmeticError):
        await update.message.reply_text("Некорректный курс.")
        return
    
    with SessionFactory() as session:
        user: User | None = session.query(User).filter_by(id=update.effective_user.id).one_or_none()
        if user is None:
            user = context.user_data.get('new_user', None)
            if user is not None:
                session.add(user)
        user.exchange_rate = new_exchange_rate
        session.commit()
            
        await update.message.reply_text(f"Курс обновлен: {user.formatted_exchange_rate} ₽.")
        await display_account(update, user, session)

    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionFactory() as session:
        user = session.query(User).filter_by(id=update.effective_user.id).one_or_none()

        if user is None:
            if update.effective_user.username is None:
                await update.effective_message.reply_text("Имя пользователя не установлено. Выберите имя пользователя в настройках.\n(Настройки -> Выбрать имя пользователя)")
                return ConversationHandler.END
            context.user_data['new_user'] = User(id=update.effective_user.id, name=update.effective_user.name)
            await update.effective_message.reply_text("Предоставьте новые реквезиты:")
            return CHANGE_CARD_DETAILS
        
        await display_account(update, user, session)

        return ConversationHandler.END
    
async def start_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionFactory() as session:
        user = session.query(User).filter_by(id=update.effective_user.id).one()
        user.is_working = True
        session.commit()
        
        await update.effective_message.delete()
        await display_account(update, user, session)

async def stop_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionFactory() as session:
        user = session.query(User).filter_by(id=update.effective_user.id).one()
        user.is_working = False
        session.commit()
        
        await update.effective_message.delete()
        await display_account(update, user, session)

async def display_account(update: Update, user: User, session):
    users: List[User] = session.query(User).order_by(User.exchange_rate).all()
    await update.effective_message.reply_text(
        f"{user.name} | {user.formatted_balance} USDT | 1 USDT = {user.formatted_exchange_rate} ₽\nРеквизиты: {user.card}\n\nTOP:\n{'\n'.join([f"{user[0]}. {user[1].formatted_name} | {user[1].formatted_balance} USDT | 1 USDT = {user[1].formatted_exchange_rate} ₽" for user in enumerate(users[:TOP_LENGTH], 1)])}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Купить USDT", callback_data=order.__name__)],
            [
                InlineKeyboardButton("Изменить курс", callback_data=HandlerNames.CHANGE_EXCHANGE_RATE),
                InlineKeyboardButton("Изменить реквизиты", callback_data=HandlerNames.CHANGE_CARD_DETAILS)
            ],
            [InlineKeyboardButton("Завершить работу", callback_data=HandlerNames.STOP_WORK)] if user.is_working else [InlineKeyboardButton("Начать работу", callback_data=HandlerNames.START_WORK)],
            [InlineKeyboardButton("Руководство", url="https://example.com")]
        ]))

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['active_conversation'] = None
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END
    

async def main():
    cancel_handler = CommandHandler("cancel", cancel)

    conv_handler_registration = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHANGE_EXCHANGE_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_exchange_rate)],
            CHANGE_CARD_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_card_details)],
        },
        fallbacks=[cancel_handler],
        persistent=False,
    )

    conv_handler_exchange_rate = ConversationHandler(
        entry_points=[CallbackQueryHandler(change_exchange_rate, pattern=HandlerNames.CHANGE_EXCHANGE_RATE)],
        states={
            CHANGE_EXCHANGE_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_exchange_rate)],
        },
        fallbacks=[cancel_handler],
        persistent=False,
    )

    conv_handler_card_details = ConversationHandler(
        entry_points=[CallbackQueryHandler(change_card_details, pattern=HandlerNames.CHANGE_CARD_DETAILS)],
        states={
            CHANGE_CARD_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_card_details)],
        },
        fallbacks=[cancel_handler],
        persistent=False,
    )

    conv_handler_deposit_usdt = ConversationHandler(
        entry_points=[CallbackQueryHandler(order, pattern=order.__name__)],
        states={
            ORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order)],
        },
        fallbacks=[cancel_handler],
        persistent=False,
    )

    application.add_handlers([conv_handler_registration, conv_handler_deposit_usdt, conv_handler_exchange_rate, conv_handler_card_details])
    application.add_handlers([CallbackQueryHandler(accept_order, pattern=HandlerNames.ACCEPT_ORDER), CallbackQueryHandler(decline_order, pattern=HandlerNames.DECLINE_ORDER)])
    application.add_handlers([CallbackQueryHandler(start_work, pattern=HandlerNames.START_WORK), CallbackQueryHandler(stop_work, pattern=HandlerNames.STOP_WORK)])
    application.add_handlers([CallbackQueryHandler(handle_order, pattern=HandlerNames.HANDLE_ORDER), CallbackQueryHandler(confirm_order, pattern=HandlerNames.CONFIRM_ORDER), CallbackQueryHandler(complete_order, pattern=HandlerNames.COMPLETE_ORDER), CallbackQueryHandler(call_support, pattern=HandlerNames.CALL_SUPPORT)])

    # Both servers .start() and not .run() so to not block the event loop on which they both must run.
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    # await application.updater.start_webhook(listen="localhost", port=80,
    #                                         webhook_url="https://1052-171-225-184-254.ngrok-free.app",
    #                                         cert="",
    #                                         key="")

    await Server(Config(app)).serve()
    
    

if __name__ == '__main__':
    run(main())
