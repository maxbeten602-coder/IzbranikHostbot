"""Точка входа для хостинга: python main.py"""
import asyncio

from bot.main import main

if __name__ == "__main__":
    asyncio.run(main())
