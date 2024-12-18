from decimal import Decimal
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('TOKEN')
API_KEY = os.getenv('API_KEY')
ACCEPT_ORDER_TIMEOUT = float(os.getenv('ACCEPT_ORDER_TIMEOUT'))
TOP_LENGTH = int(os.getenv('TOP_LENGTH'))
FROZEN_BALANCE_COOLDOWN = float(os.getenv('FROZEN_BALANCE_COOLDOWN'))
ORDER_FEE = Decimal(os.getenv('ORDER_FEE'))
SUPPORT_ID = int(os.getenv('SUPPORT_ID'))
