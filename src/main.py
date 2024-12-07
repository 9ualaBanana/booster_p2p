from creditcard import CreditCard
from dotenv import load_dotenv
import logging
import os
from sqlalchemy import create_engine, Column, Integer, Double, String
from sqlalchemy.orm import sessionmaker, declarative_base
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler

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

CHANGE_RATE, CHANGE_CARD, DEPOSIT_USDT = range(3)

async def deposit_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Please enter the amount of USDT you want to deposit:")
    
    return DEPOSIT_USDT  # Return state to indicate we're waiting for deposit amount

async def change_exchange_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Please provide the new exchange rate:")
    
    return CHANGE_RATE  # Return state to indicate we're waiting for the exchange rate

async def change_card_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Please provide your new card value:")
    
    return CHANGE_CARD  # Return state to indicate we're waiting for card input

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionFactory() as session:
        user = session.query(User).filter_by(id=update.effective_user.id).one_or_none()
        
        if user is None:
            user = User(id=update.effective_user.id, name=update.effective_user.username, balance=0, exchange_rate=0)
            session.add(user)
            session.commit()

        users = session.query(User).all()
        users.sort(key=lambda user: user.exchange_rate)

        await update.effective_message.reply_text(
            f"""
            {update.effective_user.username} | {user.balance} USDT | 1 USDT = {user.exchange_rate} ₽
            
            TOP:
            {'\n'.join([f"{user.name} | {user.balance} | {user.exchange_rate}" for user in users])}
            """,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Deposit USDT", callback_data=deposit_usdt.__name__)],
                [InlineKeyboardButton("Изменить курс", callback_data=change_exchange_rate.__name__)],
                [InlineKeyboardButton("Изменить реквизиты", callback_data=change_card_details.__name__)]
            ]))

async def receive_exchange_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_exchange_rate = float(update.message.text.strip())  # Convert input to float
        
        if new_exchange_rate < 0:
            await update.message.reply_text("Please enter a valid positive number for the exchange rate.")
            return
        
        with SessionFactory() as session:
            user = session.query(User).filter_by(id=update.effective_user.id).one_or_none()
            
            if user:
                user.exchange_rate = new_exchange_rate  # Update user's exchange rate
                session.commit()  # Commit changes to database
                
                await update.message.reply_text(f"Exchange rate updated to {new_exchange_rate} ₽.")
            else:
                await update.message.reply_text("User not found in the database.")
        
    except ValueError:
        await update.message.reply_text("Please enter a valid number for the exchange rate.")
    
    return ConversationHandler.END  # End conversation after processing

async def receive_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card = CreditCard(update.message.text.strip())

    if not card.is_valid or card.is_expired:
        await update.message.reply_text("Provided card details are invalid.")
        return ConversationHandler.END
    
    with SessionFactory() as session:
        user = session.query(User).filter_by(id=update.effective_user.id).one_or_none()
        
        if user:
            user.card = card.number  # Update user's card value
            session.commit()  # Commit changes to database
            
            await update.message.reply_text(f"Card updated to {card.number}.")
        else:
            await update.message.reply_text("User not found in the database.")
        
        return ConversationHandler.END  # End conversation after processing

async def receive_deposit_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())  # Convert input to float
        
        if amount <= 0:
            await update.message.reply_text("Please enter a valid positive amount of USDT to deposit.")
            return
        
        address = "123456789"  # Replace with actual address
        await update.message.reply_text(f"Address: {address}.")
        
    except ValueError:
        await update.message.reply_text("Please enter a valid amount of USDT.")

    return ConversationHandler.END  # End conversation after processing

def main():
    load_dotenv()
    TOKEN = os.getenv('TOKEN')
    
    application = ApplicationBuilder().token(TOKEN).build()

    # Handle wrong inputs during conversations.
    conv_handler_exchange_rate = ConversationHandler(
        entry_points=[CallbackQueryHandler(change_exchange_rate, pattern=change_exchange_rate.__name__)],
        states={
            CHANGE_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_exchange_rate)],
        },
        fallbacks=[],
        name="exchange_rate_conversation",
        persistent=False,
    )

    conv_handler_card_details = ConversationHandler(
        entry_points=[CallbackQueryHandler(change_card_details, pattern=change_card_details.__name__)],
        states={
            CHANGE_CARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_card)],
        },
        fallbacks=[],
        name="card_details_conversation",
        persistent=False,
    )

    conv_handler_deposit_usdt = ConversationHandler(
        entry_points=[CallbackQueryHandler(deposit_usdt, pattern='deposit_usdt')],
        states={
            DEPOSIT_USDT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_deposit_usdt)],
        },
        fallbacks=[],
        name="deposit_usdt_conversation",
        persistent=False,
    )

    application.add_handler(CommandHandler('start', start))
    application.add_handlers([conv_handler_deposit_usdt, conv_handler_exchange_rate, conv_handler_card_details])

    application.run_polling()

if __name__ == '__main__':
    main()
