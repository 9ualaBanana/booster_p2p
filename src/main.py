from asyncio import run, wait_for
import logging
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler
from telegram.helpers import escape_markdown as md
from fastapi import FastAPI, HTTPException, Header, Depends
from uvicorn import Config, Server
from config import SUPPORT_ID, TOKEN, API_KEY, ACCEPT_ORDER_TIMEOUT, TOP_LENGTH, ORDER_FEE
from datetime import datetime, timezone
from enum import Enum
from typing import List
from zoneinfo import ZoneInfo
from sqlalchemy import or_
from decimal import Decimal, ROUND_HALF_EVEN
from typing import List
from creditcard import CreditCard
from database import OrderStatus, SessionFactory, User, Order
from pydantic import BaseModel, field_validator
from formatting_helper import FormattingHelper
from order_manager import OrderContext, OrderContextManager

class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_data = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
            "name": record.name,
            "filename": record.filename,
            "lineno": record.lineno,
            "funcName": record.funcName,
            "process": record.process,
            "thread": record.thread,
        }
        if record.exc_info:
            log_data["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(log_data)


logging.basicConfig(level=logging.INFO, format='%(message)s')
formatter = JsonFormatter()
for handler in logging.root.handlers:
   handler.setFormatter(formatter)

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
    YES_SUPPORT = "yes_support"
    NO_SUPPORT = "no_support"
    START_WORK = "start_work"
    STOP_WORK = "stop_work"

application = ApplicationBuilder().token(TOKEN).build()
app = FastAPI()

async def validate_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")

class CreateOrderRequest(BaseModel):
    quantity: Decimal

    @field_validator('quantity')
    def validate_amount(cls, quantity):
        if quantity <= Decimal(0):
            raise ValueError("Amount must be positive.")
        return quantity.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)

@app.post("/orders", dependencies=[Depends(validate_api_key)])
async def order(order_request: CreateOrderRequest):
    logging.info(f"Order requested with quantity: {order_request.quantity}")
    with SessionFactory() as session:
        users = session.query(User).filter(
            User.is_working, 
            or_(~User.orders.any(), ~User.orders.any(Order.status != OrderStatus.COMPLETED)), 
            User.balance >= order_request.quantity
        ).order_by(User.exchange_rate).all()

    for user in users:
        try:
            logging.info(f"Creating order for user {user.name} ({user.id})")
            # Acquiring exclusive lock under which all `user_data` IO must be done.
            async with await OrderContextManager.get(user.id, application.user_data) as ocm:
                if ocm.context:
                    raise EnvironmentError(f"User with all complete order has active {OrderContext.__name__}.")
                
                async with ocm.create_context() as oc:
                    order = Order(quantity=order_request.quantity, price=user.exchange_rate)
                    oc.session.add(user)
                    user.orders.append(order)
                    oc.session.commit()
                    oc._order_id = order.id

                    oc.notification = await application.bot.send_message(user.id,
                                                        f"Запрос на покупку *{md(FormattingHelper.quantize(order_request.quantity, 8), version=2)}* USDT\nБаланс: *{md(user.formatted_balance, version=2)}* USDT\nПрибыль: *{md(FormattingHelper.quantize(order_request.quantity * user.exchange_rate, 2), version=2)}* ₽",
                                                        reply_markup=InlineKeyboardMarkup([
                                                            [InlineKeyboardButton("Принять", callback_data=HandlerNames.ACCEPT_ORDER), InlineKeyboardButton("Отклонить", callback_data=HandlerNames.DECLINE_ORDER)]
                                                            ]),
                                                            parse_mode="MarkdownV2")
        except Exception as e:
            logging.error(f"Error during order creation for user {user.name} ({user.id}): {e}", exc_info=True)
            continue


        try:
            await wait_for(oc.event.wait(), timeout=ACCEPT_ORDER_TIMEOUT)
        
            async with (await OrderContextManager.get(user.id, application.user_data)).context as oc:
                match oc.order.status:
                    case OrderStatus.ACCEPTED:
                        oc.order.user.balance -= oc.order.quantity
                        oc.order.user.frozen_balance += oc.order.quantity
                        oc.session.commit()
                        await oc.start_client_completion_waiter()

                        return {
                            "account": {
                                "id": user.id,
                                "card": user.card
                            },
                            "order": {
                                "id": oc.order.id,
                                "price": oc.order.price,
                                "quantity": oc.order.quantity
                            }
                        }
                    
                    case OrderStatus.DECLINED:
                        oc.session.delete(oc.order)
                        oc._order_id = None
                        oc.session.commit()

                    case _:
                        logging.error("Order was handled but didn't match any valid status.")
        
        except TimeoutError:
            async with (await OrderContextManager.get(user.id, application.user_data)).context as oc:
                if oc.order.status == OrderStatus.PENDING:
                    oc.order.user.balance -= ORDER_FEE
                    oc.session.delete(oc.order)
                    oc.session.commit()

                    await oc.notification.edit_text(f"Время ответа на ордер `{md(oc.order.id, version=2)}` истекло\nСервисная плата в размере *{md(FormattingHelper.quantize(ORDER_FEE, 8), version=2)}* USDT была изъята", reply_markup=None, parse_mode="MarkdownV2")

                else:
                    logging.warning(f"Handled order ({oc.order.id}) somehow reached timeout.")

        except Exception as e:
            logging.error("Order handling went wrong: {e}", exc_info=True)

        
        async with await OrderContextManager.get(user.id, application.user_data) as ocm:
            ocm.remove_context()

    # Define the reason why order can't be completed.
    # It's either none of the users accepted it or there were no matching users who could complete the order.
    logging.info("Order can't be completed. None of the users accepted it.")
    raise HTTPException(status_code=404, detail="Order can't be completed. None of the users accepted it.")

async def accept_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

    async with (await OrderContextManager.get(update.effective_user.id, application.user_data)).context as oc:
        if oc.order.status == OrderStatus.PENDING:
            oc.order.status = OrderStatus.ACCEPTED
            oc.session.commit()

            await oc.notification.edit_reply_markup(None)
            oc.support_message = await context.bot.send_message(SUPPORT_ID, f"Ордер ID: `{md(oc.order.id, version=2)}`\nСтоимость: *{md(FormattingHelper.quantize(oc.order.total_price, 2), version=2)}* ₽\nКонтрагент: @{update.effective_user.username}\nРеквизиты: `{oc.order.user.card}`", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Да ID", callback_data=f"{HandlerNames.YES_SUPPORT}|{oc.order.user_id}"), InlineKeyboardButton("Нет ID", callback_data=f"{HandlerNames.NO_SUPPORT}|{oc.order.user_id}")]
                ]),
                parse_mode="MarkdownV2")
            
            oc.event.set()

        else:
            logging.warning(f"Order can't be accepted. It has {oc.order.status} status.")

async def decline_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

    async with (await OrderContextManager.get(update.effective_user.id, application.user_data)).context as oc:
        if oc.order.status == OrderStatus.PENDING:
            oc.order.status = OrderStatus.DECLINED
            oc.session.commit()

            await oc.notification.delete()

            oc.event.set()

        else:
            logging.warning(f"Order can't be declined. It has {oc.order.status} status.")


class CompleteOrderRequest(BaseModel):
    order_id: str
    account_id: int

@app.patch("/orders", dependencies=[Depends(validate_api_key)])
async def order(request: CompleteOrderRequest):
    logging.info(f"{CompleteOrderRequest.__name__} for order {request.order_id} for account {request.account_id} from client was received.")

    async with (await OrderContextManager.get(request.account_id, application.user_data)).context as oc:
        await oc.cancel_client_completion_waiter()
        if oc.order.id == request.order_id:
            if oc.order.status == OrderStatus.ACCEPTED:
                oc.order.paid_at = datetime.now(timezone.utc)
                oc.session.commit()

                await application.bot.send_message(oc.order.user.id, f"Клиент оплатил *{md(FormattingHelper.quantize(oc.order.total_price, 2), version=2)}* ₽", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Подтвердить", callback_data=HandlerNames.CONFIRM_ORDER), InlineKeyboardButton("Обратиться в тех. поддержку", callback_data=HandlerNames.CALL_SUPPORT)]
                    ]),
                    parse_mode="MarkdownV2")
            else:
                message = f"Active order {oc.order.id} can't be completed. It has {oc.order.status} status."
                logging.error(message)
                raise HTTPException(status_code=409, detail=message)
        else:
            message = f"Active order {oc.order.id} can't be completed. Received wrong order ID for account {request.account_id}: {request.order_id}"
            logging.error(message)
            raise HTTPException(status_code=404, detail=message)
    
async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    
    await update.effective_message.edit_text(f"{update.effective_message.text_markdown_v2}\n\nВы уверены?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("Да", callback_data=HandlerNames.COMPLETE_ORDER), InlineKeyboardButton("Нет", callback_data=HandlerNames.HANDLE_ORDER)]
        ]),
        parse_mode="MarkdownV2")
    
async def handle_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"Order handling requested for order {update.effective_message.text}")
    await update.callback_query.answer()

    async with (await OrderContextManager.get(update.effective_user.id, application.user_data)).context as oc:
        await update.effective_message.edit_text(f"Клиент оплатил *{md(FormattingHelper.quantize(oc.order.total_price, 2), version=2)}* ₽", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Подтвердить", callback_data=HandlerNames.CONFIRM_ORDER), InlineKeyboardButton("Обратиться в тех. поддержку", callback_data=HandlerNames.CALL_SUPPORT)]
            ]),
            parse_mode="MarkdownV2")
        
async def complete_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

    async with (await OrderContextManager.get(update.effective_user.id, application.user_data)).context as oc:
        if oc.order.status == OrderStatus.ACCEPTED:
            oc.order.user.frozen_balance -= oc.order.quantity
            oc.order.status = OrderStatus.COMPLETED
            oc.session.commit()
            await update.effective_message.delete()
            async with await OrderContextManager.get(update.effective_user.id, application.user_data) as ocm:
                ocm.remove_context()
            logging.info(f"Order {oc.order.id} completed for user {update.effective_user.id}")
            # Send confirmation back to the client.

async def call_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"Support call requested for order {update.effective_message.text}")
    await update.callback_query.answer()

    async with (await OrderContextManager.get(update.effective_user.id, application.user_data)).context as oc:
        await update.effective_message.reply_markdown_v2(f"@techsupport\n\n*Ордер ID*: `{oc.order.id}`\n*Время оплаты*: `{oc.order.paid_at.astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M:%S")}`")

async def yes_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"Yes support called with {update.callback_query.data}")
    await update.callback_query.answer()

    async with (await OrderContextManager.get(int(update.callback_query.data.split('|')[-1]), application.user_data)).context as oc:
        if oc.order.status == OrderStatus.ACCEPTED:
            logging.info(f"Order {oc.order.id} completed by support for user {update.effective_user.id}")
            oc.order.user.frozen_balance -= oc.order.quantity
            oc.order.status = OrderStatus.COMPLETED
            oc.session.commit()
            await update.effective_message.delete()
            async with await OrderContextManager.get(update.effective_user.id, application.user_data) as ocm:
                ocm.remove_context()
            # Send confirmation back to the client.

async def no_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"No support called with {update.callback_query.data}")
    await update.callback_query.answer()

    async with (await OrderContextManager.get(int(update.callback_query.data.split('|')[-1]), application.user_data)).context as oc:
        if oc.order.status == OrderStatus.ACCEPTED:
            logging.info(f"Order {oc.order.id} rejected by support for user {update.effective_user.id}")
            oc.order.user.frozen_balance -= oc.order.quantity
            oc.order.user.balance += oc.order.quantity
            oc.session.delete(oc.order)
            oc.session.commit()
            await update.effective_message.delete()
            async with await OrderContextManager.get(update.effective_user.id, application.user_data) as ocm:
                ocm.remove_context()
            # Send rejection back to the client.
    
async def order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"User {update.effective_user.id} requested to start order")
    await update.callback_query.answer()

    await update.effective_message.reply_text("Сколько USDT вы хотите купить?")
    await update.effective_message.delete()
    
    return ORDER

async def receive_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = Decimal(update.message.text.strip()).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)
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
            logging.info(f"User {user.id} balance updated with {amount}")
            await update.message.reply_text(f"Баланс пополнен: {user.formatted_balance} USDT.")
        else:
            await update.message.reply_text("Аккаунт не найден.")
            # Must be impossible. Redirect to registration.

    return ConversationHandler.END

async def change_card_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"User {update.effective_user.id} requested to change card details")
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
                        logging.info(f"User card details changed to {card.number}")
                        await update.message.reply_text(f"Реквизиты успешно изменены {card.number}.")
                        await update.effective_message.reply_text("Предоставьте новый курс:")
                        return CHANGE_EXCHANGE_RATE
                else:
                    user.card = card.number
                    session.commit()
                    logging.info(f"User card details changed to {card.number}")
                    await update.message.reply_text(f"Реквизиты успешно изменены {card.number}.")
                    await display_account(update, user, session)

                    return ConversationHandler.END
    
    await update.message.reply_text("Предоставленные реквезиты некорректны.")

async def change_exchange_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"User {update.effective_user.id} requested to change exchange rate")
    await update.callback_query.answer()
    await update.effective_message.reply_text("Предоставьте новый курс:")
    await update.effective_message.delete()
    
    return CHANGE_EXCHANGE_RATE

async def receive_exchange_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_exchange_rate = Decimal(update.message.text.strip()).quantize(Decimal('0.000001'), rounding=ROUND_HALF_EVEN)
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
            
        logging.info(f"User {user.id} exchange rate changed to {new_exchange_rate}")
        await update.message.reply_text(f"Курс обновлен: {user.formatted_exchange_rate} ₽.")
        await display_account(update, user, session)

    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"User {update.effective_user.id} started bot")
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
    logging.info(f"User {update.effective_user.id} started work")
    with SessionFactory() as session:
        user = session.query(User).filter_by(id=update.effective_user.id).one()
        user.is_working = True
        session.commit()
        
        await update.effective_message.delete()
        await display_account(update, user, session)

async def stop_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"User {update.effective_user.id} stopped work")
    with SessionFactory() as session:
        user = session.query(User).filter_by(id=update.effective_user.id).one()
        user.is_working = False
        session.commit()
        
        await update.effective_message.delete()
        await display_account(update, user, session)

async def display_account(update: Update, user: User, session):
    users: List[User] = session.query(User).order_by(User.exchange_rate).all()
    await update.effective_message.reply_markdown_v2(
        f"{user.name} | *{user.formatted_balance}* USDT | 1 USDT = *{user.formatted_exchange_rate}* ₽\nРеквизиты: `{user.card}`\n\n*TOP*:\n{'\n'.join([f"{user[0]}. {user[1].formatted_name} | *{user[1].formatted_balance}* USDT | 1 USDT = *{user[1].formatted_exchange_rate}* ₽" for user in enumerate(users[:TOP_LENGTH], 1)])}",
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
    application.add_handlers([CallbackQueryHandler(yes_support, pattern=f"{HandlerNames.YES_SUPPORT}|(d+)"), CallbackQueryHandler(no_support, pattern=f"{HandlerNames.NO_SUPPORT}|(d+)")])

    # Both servers .start() and not .run() so to not block the event loop on which they both must run.
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    await Server(Config(app)).serve()
    
if __name__ == '__main__':
    run(main())
