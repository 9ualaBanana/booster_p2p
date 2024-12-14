from asyncio import Event, Lock
from telegram import Message

class OrderController:
    def __init__(self, notification: Message):
        self.notification = notification
        self.lock = Lock()
        self.event = Event()
