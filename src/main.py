from config import TOKEN, API_KEY, ACCEPT_ORDER_TIMEOUT, TOP_LENGTH
from asyncio import run, wait_for
from creditcard import CreditCard
from database import SessionFactory, User
from fastapi import FastAPI, HTTPException, Header, Depends
import logging
from order import Order
from pydantic import BaseModel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler
from uvicorn import Config, Server

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)

CHANGE_EXCHANGE_RATE, CHANGE_CARD_DETAILS, BUY_USDT = range(3)

application = ApplicationBuilder().token(TOKEN).build()
app = FastAPI()

async def validate_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")

class BuyRequest(BaseModel):
    amount: float

@app.post("/buy_usdt", dependencies=[Depends(validate_api_key)])
async def buy_usdt(request: BuyRequest):
    if request.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")

    with SessionFactory() as session:
        users = session.query(User).filter(User.balance >= request.amount).order_by(User.exchange_rate).all()

        for user in users:
            message = await application.bot.send_message(user.id,
                f"Запрос на покупку {request.amount} USDT\nБаланс: {user.balance} USDT\nПрибыль: {request.amount * user.exchange_rate} ₽",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("Принять", callback_data=accept_order.__name__),
                        InlineKeyboardButton("Отклонить", callback_data=decline_order.__name__)]
                    ]))

            # Implement inner dictionary for orders inside bot_data.
            order = Order()
            application.bot_data[message.id] = order

            try:
                # Implement buy/sell transactions.
                # * Add balance after Deposit USDT transaction is completed and confirmed. (consider smart contract)
                # * Freeze balance if buy order is accepted.
                await wait_for(order.event.wait(), timeout=ACCEPT_ORDER_TIMEOUT)
                
                if order.state is Order.State.ACCEPTED:
                    user.balance -= request.amount
                    user.frozen_balance += request.amount
                    session.commit()
                    
                    return {
                        "price": request.amount * user.exchange_rate,
                        "card": user.card
                    }
                
            except TimeoutError:
                await application.bot.delete_message(message.chat_id, message.id)
                application.bot_data.pop(message.id, None)
                continue

        raise HTTPException(status_code=404, detail="Order can't be completed. None of the users accepted it.")

async def accept_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    order = context.bot_data.pop(update.effective_message.id, None)
    
    if isinstance(order, Order):
        order.state = Order.State.ACCEPTED
    else:
        raise ValueError("Order waiting for accepted order was expected.")
    
async def decline_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    order = context.bot_data.pop(update.effective_message.id, None)
    
    if isinstance(order, Order):
        order.state = Order.State.DECLINED
        # Additional cleanup if necessary
        # await application.bot.delete_message(update.effective_chat.id, update.effective_message.id)
    else:
        raise ValueError("Order waiting for declined order was expected.")
    


async def buy_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.effective_message.reply_text("Сколько USDT вы хотите купить?")
    await update.effective_message.delete()
    
    return BUY_USDT

async def receive_buy_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            await update.message.reply_text("Некорректная сумма USDT.")
            return
    except ValueError:
        await update.message.reply_text("Некорректная сумма USDT.")
        return
        
    address = "123456789"  # Replace with actual address
    await update.message.reply_text(f"Адрес: {address}.")

    # Move to actual handler that will validate deposit transaction.
    with SessionFactory() as session:
        user = session.query(User).filter_by(id=update.effective_user.id).one_or_none()
        
        if user:
            user.balance += amount
            session.commit()
            
            await update.message.reply_text(f"Баланс пополнен: {user.balance} USDT.")
        else:
            await update.message.reply_text("Аккаунт не найден.")
            # Must be impossible. Redirect to registration.

    return ConversationHandler.END

async def change_card_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.effective_message.reply_text("Предоставьте новые реквезиты:")
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
        new_exchange_rate = float(update.message.text.strip())
        if new_exchange_rate <= 0:
            await update.message.reply_text("Некорректный курс.")
            return
    except ValueError:
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
            
        await update.message.reply_text(f"Курс обновлен: {new_exchange_rate} ₽.")
        await display_account(update, user, session)

    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionFactory() as session:
        user = session.query(User).filter_by(id=update.effective_user.id).one_or_none()

        if user is None:
            context.user_data['new_user'] = User(id=update.effective_user.id, name=update.effective_user.name)
            await update.effective_message.reply_text("Предоставьте новые реквезиты:")
            return CHANGE_CARD_DETAILS
        
        await display_account(update, user, session)

        return ConversationHandler.END

async def display_account(update: Update, user: User, session):
    users = session.query(User).order_by(User.exchange_rate).all()

    await update.effective_message.reply_text(
        f"{user.name} | {user.balance} USDT | 1 USDT = {user.exchange_rate} ₽\nРеквизиты: {user.card}\n\nTOP:\n{'\n'.join([f"{user[0]}. {user[1].formatted_name} | {user[1].balance} USDT | 1 USDT = {user[1].exchange_rate} ₽" for user in enumerate(users[:TOP_LENGTH], 1)])}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Купить USDT", callback_data=buy_usdt.__name__)],
            [InlineKeyboardButton("Изменить курс", callback_data=change_exchange_rate.__name__)],
            [InlineKeyboardButton("Изменить реквизиты", callback_data=change_card_details.__name__)]
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
        entry_points=[CallbackQueryHandler(change_exchange_rate, pattern=change_exchange_rate.__name__)],
        states={
            CHANGE_EXCHANGE_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_exchange_rate)],
        },
        fallbacks=[cancel_handler],
        persistent=False,
    )

    conv_handler_card_details = ConversationHandler(
        entry_points=[CallbackQueryHandler(change_card_details, pattern=change_card_details.__name__)],
        states={
            CHANGE_CARD_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_card_details)],
        },
        fallbacks=[cancel_handler],
        persistent=False,
    )

    conv_handler_deposit_usdt = ConversationHandler(
        entry_points=[CallbackQueryHandler(buy_usdt, pattern=buy_usdt.__name__)],
        states={
            BUY_USDT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_buy_usdt)],
        },
        fallbacks=[cancel_handler],
        persistent=False,
    )

    application.add_handlers([conv_handler_registration, conv_handler_deposit_usdt, conv_handler_exchange_rate, conv_handler_card_details])
    application.add_handler(CallbackQueryHandler(accept_order, pattern=accept_order.__name__))
    application.add_handler(CallbackQueryHandler(decline_order, pattern=decline_order.__name__))

    # Both servers .start() and not .run() so to not block the event loop on which they both must run.
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    await Server(Config(app)).serve()
    
    

if __name__ == '__main__':
    run(main())
