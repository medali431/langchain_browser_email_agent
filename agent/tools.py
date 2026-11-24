"""Outils réutilisables : Tavily, Crawl4AI, Gmail SMTP et LLM critique."""
import os, ssl, smtplib, asyncio, requests, json
from email.mime.text import MIMEText    
from dotenv import load_dotenv
from crawl4ai import AsyncWebCrawler,BrowserConfig,CrawlerRunConfig,CacheMode
from crawl4ai.models import CrawlResult
from agent.llm import create_llm
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from crawl4ai.content_filter_strategy import PruningContentFilter
from langchain_tavily import TavilySearch
import os
from agent.models import Director
import logging
from pydantic import BaseModel, ValidationError
from typing import List
import re

logger = logging.getLogger("tools_logger")

load_dotenv()

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

# Configuration Windows-friendly pour Crawl4AI
if os.name == "nt":
    # Configuration spéciale pour Windows - évite les problèmes avec Playwright
    browser_config = BrowserConfig(
        browser_type="chromium",
        headless=True,
        user_agent=DEFAULT_UA,
        ignore_https_errors=True,
        java_script_enabled=True,
        use_managed_browser=False,  # Évite les problèmes de subprocess sur Windows
    )
    config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        delay_before_return_html=5,
        excluded_tags=["nav","footer","script","style"]
    )
else:
    # Configuration standard pour Linux/Mac
    browser_config = BrowserConfig(
        browser_type="chromium",
        headless=True,
        user_agent=DEFAULT_UA,
    )
    config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)

# --- Variables d'environnement -------------------------------------------------
def _tavily_tool(k: int):
    """Construit l'outil Tavily à partir de la clé configurée."""
    if not os.getenv("TAVILY_API_KEY"):
        raise EnvironmentError("Configurez TAVILY_API_KEY dans le fichier .env.")
    return TavilySearch(max_results=k)


def _format_tavily_results(results) -> str:
    if isinstance(results, str):
        results = json.loads(results)
    if isinstance(results, dict):
        results = results.get("results", [])
    if not results:
        return "Aucun résultat trouvé."
    return "\n".join(
        f"- [{item.get('title', 'Titre inconnu')}]({item.get('url', '#')})"
        + (f"\n  {item['content'][:800]}" if item.get("content") else "")
        for item in results
    )


def tavily_search(query: str, k: int = 5) -> str:
    """Renvoie les premiers résultats Tavily au format Markdown."""
    try:
        logger.info("Tavily search: %s", query)
        return _format_tavily_results(
            _tavily_tool(k).invoke({"query": query})
        )
    except Exception as e:
        logger.exception("Erreur lors de la recherche Tavily pour %r", query)
        return f"Erreur Tavily : {e}"


# ---------------------------------------------------------------------------
# Version asynchrone pour éviter les appels bloquants depuis une boucle async
# ---------------------------------------------------------------------------


async def atavily_search(query: str, k: int = 5) -> str:
    """Version asynchrone de la recherche Tavily pour les nœuds LangGraph."""
    try:
        logger.info("Tavily search (async): %s", query)
        return _format_tavily_results(
            await _tavily_tool(k).ainvoke({"query": query})
        )
    except Exception as e:
        logger.exception("Erreur async Tavily pour %r", query)
        return f"Erreur Tavily : {e}"

def _sync_crawl_markdown(url: str) -> str:
    """Version synchrone du crawling pour éviter les blocages."""
    import asyncio
    
    async def _do_crawl():
        try:
            # Utiliser la configuration Windows-friendly
            async with AsyncWebCrawler(
                config=browser_config,
                always_by_pass_cache=True,
                thread_safe=True
            ) as crawler:
                result = await crawler.arun(url, config=config)
                if result.status_code == 404:
                    logger.error(f"Page non trouvée: {url}")
                    return "Page non trouvée."
                return result.markdown
        except Exception as crawl_error:
            logger.warning(f"Crawl4AI a échoué pour {url}: {crawl_error}")
            # Fallback : simple requête HTTP si Crawl4AI échoue
            try:
                response = requests.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=10)
                response.raise_for_status()
                return f"Contenu brut de {url}:\n\n{response.text[:5000]}..."
            except Exception as req_error:
                logger.error(f"Fallback requests a également échoué: {req_error}")
                return f"Impossible d'accéder à {url}: {str(crawl_error)}"
    
    try:
        # Créer une nouvelle boucle d'événements pour ce thread avec la policy correcte
        loop = asyncio.new_event_loop()
        if os.name == "nt":
            # Important : ProactorEventLoop (par défaut) supporte les subprocess ;
            # on veille à conserver cette policy plutôt que Selector.
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())  # type: ignore[attr-defined]
            except AttributeError:
                # Version de Python plus ancienne (<3.8) : on laisse la policy courante
                pass
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(_do_crawl())
        finally:
            loop.close()
    except Exception as e:
        logger.error(f"Erreur lors du crawling de {url}: {e}")
        return f"Erreur lors du crawling: {str(e)}"

def _sync_crawl_dirigeants(page_url: str) -> list[Director]:
    """
    Version synchrone (thread-safe) pour récupérer les dirigeants d'une page,
    inspirée de `_sync_crawl_markdown`. Elle exécute le crawler dans une nouvelle
    boucle d'événements afin d'éviter l'erreur `NotImplementedError` que l'on
    rencontre sous Windows lorsque `asyncio.create_subprocess_exec` est appelé
    depuis Playwright.
    """
    import asyncio

    async def _do_crawl_dirigeants() -> list[Director]:
        

        # 2. Lancer le crawler (Playwright) de façon Windows-friendly
        try:
            async with AsyncWebCrawler(
                config=browser_config,
                always_by_pass_cache=True,
                thread_safe=True,
            ) as crawler:
                result = await crawler.arun(page_url, config=config)
        except Exception as crawl_error:
            logger.error(f"Crawl4AI a échoué pour {page_url}: {crawl_error}")
            return []

        if not result.success:
            logger.error(result.error_message or "Crawl failed Dirigeants")
            return []

        markdown_text = result.markdown

        # --- 1. Isoler la section "Dirigeants" pour réduire les faux-positifs ---
        #   On capture tout ce qui se trouve après le titre `## Dirigeants` (ou variante
        #   d’orthographe/casse) jusqu’au prochain titre de niveau 2 (`##`) ou la fin
        #   du document. Cela évite de récupérer des éléments sans rapport comme
        #   « Publication du bilan », « Voir la fiche », etc.
        section_match = re.search(
            r"^##\s*Dirigeant[\w\s]*\n([\s\S]*?)(?=^##\s|\Z)",
            markdown_text,
            re.IGNORECASE | re.MULTILINE,
        )
        if section_match:
            markdown_text = section_match.group(1)

        logger.info(f"Crawl4AI results (section Dirigeants): {markdown_text}")

        directors: list[Director] = []

        # a) Noms sous forme de lien : [Jean Dupont](https://… "titre optionnel")
        #    - On capture uniquement l'URL (jusqu'à l'espace ou la fin de la parenthèse)
        link_pattern = re.compile(
            r"\[([A-ZÀ-ÖØ-Ý][\wÀ-ÖØ-öø-ÿ'’\- ]{2,})\]\((https?://[^\s\)]+)"  # groupe 1 = nom, groupe 2 = URL
        )
        for full_name, link in link_pattern.findall(markdown_text):
            prenom, *rest = full_name.strip().split()
            # On ignore les entrées non pertinentes (un seul mot ou mot-clé technique)
            if (not rest) or prenom.lower() in {"voir", "obtenir", "publication", "calculer"}:
                continue
            nom = " ".join(rest)
            try:
                clean_link = link.replace('</', '')
                directors.append(Director(first_name=prenom, last_name=nom, links=[clean_link]))
            except ValidationError:
                # Ignore les cas invalide (ex: caractères spéciaux inattendus)
                pass

        # b) Noms simples dans des listes : « - Jean Dupont »
        plain_pattern = re.compile(
            r"^[\*\-\+] +([A-ZÀ-ÖØ-Ý][\wÀ-ÖØ-öø-ÿ'’\- ]{2,})$",
            re.MULTILINE,
        )
        for full_name in plain_pattern.findall(markdown_text):
            prenom, *rest = full_name.strip().split()
            # On s'assure qu'il y a au moins un nom de famille et que le prénom n'est pas un mot-clé non pertinent
            if (not rest) or prenom.lower() in {"voir", "obtenir", "publication", "calculer"}:
                continue
            nom = " ".join(rest)
            try:
                directors.append(Director(first_name=prenom, last_name=nom))
            except ValidationError:
                pass

        # 5. Déduplication simple (prenom+nom+url) en conservant l’ordre
        seen = set()
        deduped: list[Director] = []
        for d in directors:
            key = (
                d.first_name.lower(),
                d.last_name.lower(),
                d.links[0] if d.links else "",
            )
            if key not in seen:
                seen.add(key)
                deduped.append(d)
        return deduped

    # 6. Exécution dans un thread avec une boucle d'événements dédiée
    try:
        loop = asyncio.new_event_loop()
        if os.name == "nt":
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())  # type: ignore[attr-defined]
            except AttributeError:
                pass
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(_do_crawl_dirigeants())
        finally:
            loop.close()
    except Exception as e:
        logger.error(f"Erreur lors du crawling de dirigeants {page_url}: {e}")
        return []


async def crawl_dirigeants(page_url: str) -> list[Director]:
    """
    Wrapper asynchrone qui exécute la version synchrone dans un thread afin
    d'isoler la création de la boucle d'événements et d'éviter les conflits
    avec la boucle principale de LangGraph.
    """
    try:
        return await asyncio.to_thread(_sync_crawl_dirigeants, page_url)
    except Exception as e:
        logger.error(f"Erreur (async) lors du crawling de dirigeants {page_url}: {e}")
        return []




async def crawl_markdown(url: str) -> str:
    """
    Crawl une page web et retourne le contenu en Markdown.
    Utilise asyncio.to_thread pour éviter les blocages lors de l'initialisation de AsyncWebCrawler.
    """
    try:
        return await asyncio.to_thread(_sync_crawl_markdown, url)
    except Exception as e:
        logger.error(f"Erreur lors du crawling de {url}: {e}")
        return f"Erreur lors du crawling: {str(e)}"


def send_gmail(to_email: str, subject: str, body: str) -> None:
    if not body or not body.strip():
        raise ValueError("Le corps de l'e-mail est vide ; aucun message n'a été envoyé.")
    gmail_addr = (os.getenv("GMAIL_ADDRESS") or "").strip()
    # Google affiche parfois le mot de passe d'application en groupes séparés.
    gmail_pass = "".join((os.getenv("GMAIL_APP_PASSWORD") or "").split())
    if not gmail_addr or not gmail_pass:
        raise EnvironmentError(
            "Configurez GMAIL_ADDRESS et GMAIL_APP_PASSWORD pour envoyer un e-mail."
        )
    if len(gmail_pass) != 16:
        raise ValueError(
            "GMAIL_APP_PASSWORD doit contenir le mot de passe d'application Google "
            "de 16 caractères, sans utiliser le mot de passe normal du compte Gmail."
        )

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, gmail_addr, to_email
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as smtp:
            smtp.login(gmail_addr, gmail_pass)
            smtp.sendmail(gmail_addr, [to_email], msg.as_string())
        logger.info("E-mail transmis à Gmail (corps: %s caractères).", len(body))
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(
            "Gmail a refusé l'authentification. Vérifiez l'adresse et régénérez "
            "le mot de passe d'application Google."
        ) from exc
    except smtplib.SMTPServerDisconnected as exc:
        raise RuntimeError(
            "Gmail a fermé la connexion pendant l'authentification. Vérifiez que "
            "GMAIL_APP_PASSWORD est un mot de passe d'application Google valide."
        ) from exc

# === Critic : simple LLM « OK / NO » =========================================
_llm_critic = create_llm()


def critic(llm_or_draft, draft: str | None = None) -> str:
    """Évalue un brouillon d'email.

    Utilisation flexible :
        critic(llm, draft)  # signature historique
        critic(draft)       # utilise un LLM par défaut
    Renvoie "YES" ou "NO" en majuscules.
    """

    # Permet d'appeler la fonction avec soit (llm, draft) soit (draft).
    if draft is None:
        draft = llm_or_draft
        llm = _llm_critic
    else:
        llm = llm_or_draft

    prompt = (
        "Réponds uniquement par YES ou NO.\n"
        "Le mail suivant est-il professionnel, clair et personnalisé ?\n\n"
        f"{draft}\n\nAnswer:"
    )
    return llm.invoke(prompt).content.strip().upper()

# ---------------------------------------------------------------------------
# Correctif Windows : l'event-loop par défaut (Proactor) ne gère pas
# asyncio.create_subprocess_exec → Playwright lève NotImplementedError.
# On force donc le SelectorEventLoop compatible subprocess.
# ---------------------------------------------------------------------------
if os.name == "nt":
    # On s'assure d'utiliser la ProactorEventLoop qui prend en charge les subprocess
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())  # type: ignore[attr-defined]
        logger.info("WindowsProactorEventLoopPolicy appliquée (support subprocess Playwright).")
    except AttributeError:
        # Python < 3.8 : WindowsProactorEventLoopPolicy peut ne pas exister
        logger.warning("WindowsProactorEventLoopPolicy indisponible ; la policy courante est conservée.")
