"""Initialisation du package agent.

Ne pas remplacer la politique asyncio Windows ici : le serveur LangGraph a
besoin de ProactorEventLoop pour lancer des sous-processus depuis Studio.
"""
