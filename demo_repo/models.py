"""Plain data models for the internal admin API."""
from dataclasses import dataclass


@dataclass
class User:
    id: int
    email: str
    display_name: str


@dataclass
class Resource:
    id: int
    owner_id: int
    name: str
