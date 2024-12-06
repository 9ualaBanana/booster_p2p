from dotenv import load_dotenv
import logging
import os
from telegram.ext import ApplicationBuilder

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

if __name__ == '__main__':
    load_dotenv()
    TOKEN = os.getenv('TOKEN')
    application = ApplicationBuilder().token(TOKEN).build()
    
    application.run_polling()
