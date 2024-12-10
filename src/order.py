from asyncio import Event
from enum import Enum

class Order:
    class State(Enum):
        PENDING = "pending"
        ACCEPTED = "accepted"
        DECLINED = "declined"

    def __init__(self):
        self._event = Event()
        self._state = Order.State.PENDING

    @property
    def event(self) -> Event:
        return self._event
    
    @property
    def state(self) -> State:
        return self._state

    @state.setter
    def state(self, new_state: State):
        self._state = new_state
        self._event.set()
