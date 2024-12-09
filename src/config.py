import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('TOKEN')
API_KEY = os.getenv('API_KEY')
ACCEPT_ORDER_TIMEOUT = float(os.getenv('ACCEPT_ORDER_TIMEOUT'))
TOP_LENGTH = int(os.getenv('TOP_LENGTH'))
