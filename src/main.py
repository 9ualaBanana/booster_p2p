from asyncio import Event, run, wait_for
from creditcard import CreditCard
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
import logging
import os
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, Double, String
from sqlalchemy.orm import sessionmaker, declarative_base
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler
from uvicorn import Config, Server

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    name = Column(String)
    card = Column(String)
    balance = Column(Double)
    frozen_balance = Column(Double)
    exchange_rate = Column(Double)

# engine = create_engine('postgresql+psycopg://postgres:admin@localhost/p2p')
engine = create_engine('sqlite:///p2p.db')
Base.metadata.create_all(engine)
SessionFactory = sessionmaker(bind=engine)

load_dotenv()
TOKEN = os.getenv('TOKEN')
ACCEPT_ORDER_TIMEOUT = float(os.getenv('ACCEPT_ORDER_TIMEOUT'))
    
CHANGE_EXCHANGE_RATE, CHANGE_CARD_DETAILS, DEPOSIT_USDT = range(3)

application = ApplicationBuilder().token(TOKEN).build()
app = FastAPI()

class DepositRequest(BaseModel):
    amount: float

@app.post("/buy_usdt")
async def deposit_usdt(request: DepositRequest):
    if request.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")

    with SessionFactory() as session:
        users = session.query(User).filter(User.balance >= request.amount).order_by(User.exchange_rate).all()

        for user in users:
            message = await application.bot.send_message(user.id,
                f"Запрос на покупку {request.amount} USDT\nБаланс: {user.balance} USDT\nПрибыль: {request.amount * user.exchange_rate} ₽",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Принять", callback_data=accept_order.__name__)]
                    ]))

            event = Event()
            application.bot_data[message.id] = event
            try:
                await wait_for(event.wait(), timeout=ACCEPT_ORDER_TIMEOUT)
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
    event = context.bot_data.pop(update.effective_message.id, None)
    if isinstance(event, Event):
        event.set()
    else:
        raise ValueError("Event waiting for accepted order was expected.")


async def deposit_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Сколько USDT вы хотите купить?")
    
    return DEPOSIT_USDT

async def receive_deposit_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    return ConversationHandler.END

async def change_exchange_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Предоставьте новый курс:")
    
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
        user = session.query(User).filter_by(id=update.effective_user.id).one_or_none()
        
        if user:
            user.exchange_rate = new_exchange_rate
            session.commit()
            
            await update.message.reply_text(f"Курс обновлен: {new_exchange_rate} ₽.")
        else:
            await update.message.reply_text("Аккаунт не найден.")
    
    return ConversationHandler.END

async def change_card_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Предоставьте новые реквезиты:")
    
    return CHANGE_CARD_DETAILS

async def receive_card_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card = CreditCard(update.message.text.strip())

    if not card.is_valid or card.is_expired:
        await update.message.reply_text("Предоставленные реквезиты некорректны.")
        return
    
    with SessionFactory() as session:
        user = session.query(User).filter_by(id=update.effective_user.id).one_or_none()
        
        if user:
            user.card = card.number
            session.commit()
            
            await update.message.reply_text(f"Реквизиты успешно изменены {card.number}.")
        else:
            await update.message.reply_text("Аккаунт не найден.")
        
        if user.exchange_rate == 0:
            await update.effective_message.reply_text("Предоставьте новый курс:")
            return CHANGE_EXCHANGE_RATE
        else:
            return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionFactory() as session:
        user = session.query(User).filter_by(id=update.effective_user.id).one_or_none()
        
        if user is None:
            user = User(id=update.effective_user.id, name=update.effective_user.name, balance=0, frozen_balance=0, exchange_rate=0)
            session.add(user)
            session.commit()
        
        if user.card is None:
            await update.effective_message.reply_text("Предоставьте новые реквизиты:")
            return CHANGE_CARD_DETAILS

        if user.exchange_rate == 0:
            await update.effective_message.reply_text("Предоставьте новый курс:")
            return CHANGE_EXCHANGE_RATE

        users = session.query(User).order_by(User.exchange_rate).all()

        await update.effective_message.reply_text(
            f"{update.effective_user.name} | {user.balance} USDT | 1 USDT = {user.exchange_rate} ₽\n\nTOP:\n{'\n'.join([f"{user[0]}. {user[1].name} | {user[1].balance} USDT | 1 USDT = {user[1].exchange_rate} ₽" for user in enumerate(users, 1)])}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Купить USDT", callback_data=deposit_usdt.__name__)],
                [InlineKeyboardButton("Изменить курс", callback_data=change_exchange_rate.__name__)],
                [InlineKeyboardButton("Изменить реквизиты", callback_data=change_card_details.__name__)]
            ]))


async def main():
    # Handle wrong inputs during conversations.
    conv_handler_registration = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHANGE_EXCHANGE_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_exchange_rate)],
            CHANGE_CARD_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_card_details)],
        },
        fallbacks=[],
        persistent=False
    )

    conv_handler_exchange_rate = ConversationHandler(
        entry_points=[CallbackQueryHandler(change_exchange_rate, pattern=change_exchange_rate.__name__)],
        states={
            CHANGE_EXCHANGE_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_exchange_rate)],
        },
        fallbacks=[],
        persistent=False
    )

    conv_handler_card_details = ConversationHandler(
        entry_points=[CallbackQueryHandler(change_card_details, pattern=change_card_details.__name__)],
        states={
            CHANGE_CARD_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_card_details)],
        },
        fallbacks=[],
        persistent=False,
    )

    conv_handler_deposit_usdt = ConversationHandler(
        entry_points=[CallbackQueryHandler(deposit_usdt, pattern=deposit_usdt.__name__)],
        states={
            DEPOSIT_USDT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_deposit_usdt)],
        },
        fallbacks=[],
        persistent=False,
    )

    application.add_handlers([conv_handler_registration, conv_handler_deposit_usdt, conv_handler_exchange_rate, conv_handler_card_details])
    application.add_handler(CallbackQueryHandler(accept_order, pattern=accept_order.__name__))

    # Both servers .start() and not .run() so to not block the event loop on which they both must run.
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    await Server(Config(app)).serve()
    
    

if __name__ == '__main__':
    run(main())
