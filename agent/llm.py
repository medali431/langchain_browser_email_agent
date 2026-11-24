import os

from dotenv import load_dotenv
from langchain_groq import ChatGroq


def create_llm(*, streaming: bool = False) -> ChatGroq:
    load_dotenv()

    if not os.getenv("GROQ_API_KEY"):
        raise EnvironmentError("Veuillez définir GROQ_API_KEY dans votre fichier .env.")

    model = (os.getenv("GROQ_MODEL") or "").strip()
    if not model or "modele_disponible" in model.lower() or "votre_compte" in model.lower():
        model = "openai/gpt-oss-20b"

    return ChatGroq(
        model=model,
        streaming=streaming,
        model_kwargs={"max_completion_tokens": 1500},
    )
