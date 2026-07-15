"""Logique de découpage et de renommage intelligent des PDF de publipostage.

Prend un PDF contenant plusieurs attestations individuelles (issu d'un
publipostage), identifie via Google Gemini les bornes de pages de chaque
attestation ainsi que le nom/prénom de la personne concernée, puis génère un
PDF par personne nommé `NOM_Prenom_AttestationEmployeur.pdf` (ou
`..._AttestationOF.pdf`).
"""

from __future__ import annotations

import io
import json
import re
import time
import unicodedata
from dataclasses import dataclass

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pypdf import PdfReader, PdfWriter

# gemini-2.5-flash n'est plus disponible pour les nouveaux comptes API
# Google (l'API renvoie une 404 les invitant à utiliser un modèle plus
# récent) : on reste donc sur gemini-3.5-flash, gratuit et à jour.
MODEL = "gemini-3.5-flash"

# Modèle de secours, utilisé si MODEL reste indisponible après toutes ses
# tentatives (souvent moins sollicité, donc plus de chances d'aboutir).
FALLBACK_MODEL = "gemini-3.1-flash-lite"

# Délais (secondes) avant chaque nouvelle tentative en cas de surcharge
# temporaire ou de non-réponse du serveur Gemini.
RETRY_DELAYS = (5, 15, 30)
FALLBACK_RETRY_DELAYS = (5, 15)


class _ModelUnavailable(Exception):
    """Levée en interne quand un modèle donné doit être abandonné (quota
    quotidien épuisé, ou toutes les tentatives de nouvelle connexion ont
    échoué) — signal pour basculer sur le modèle de secours."""

    def __init__(self, cause: Exception | None) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _generate_with_retries(
    client: genai.Client,
    model: str,
    prompt: str,
    config: types.GenerateContentConfig,
    delays: tuple[int, ...],
):
    last_error: Exception | None = None
    for delay in (0,) + delays:
        if delay:
            time.sleep(delay)
        try:
            return client.models.generate_content(model=model, contents=prompt, config=config)
        except genai_errors.ClientError as exc:
            code = getattr(exc, "code", None)
            if code == 429:
                # Quota gratuit quotidien épuisé pour CE modèle précis :
                # attendre ne sert à rien, on bascule tout de suite sur le
                # modèle de secours (qui a son propre quota séparé).
                raise _ModelUnavailable(exc) from exc
            if code != 499:
                # Erreur définitive (requête invalide, modèle introuvable...) :
                # ni la nouvelle tentative ni le changement de modèle n'y feront rien.
                raise
            # 499 CANCELLED : la requête a été interrompue avant la fin (ex :
            # notre propre timeout HTTP a coupé la connexion pendant que
            # Gemini travaillait encore) — transitoire, on retente ce modèle.
            last_error = exc
        except genai_errors.ServerError as exc:
            last_error = exc
        except httpx.TimeoutException as exc:
            last_error = exc
    raise _ModelUnavailable(last_error)


ATTESTATION_LABELS = {
    "employeur": "AttestationEmployeur",
    "of": "AttestationOF",
}

SYSTEM_PROMPT = """Tu reçois le texte extrait de chaque page d'un PDF généré par publipostage (mail merge).
Ce PDF contient une série d'attestations individuelles, chacune faisant 1 ou 2 pages, concaténées les unes à la suite des autres.

Ta tâche : regrouper les pages consécutives appartenant à la même personne, et pour chaque groupe extraire :
- son NOM de famille
- son Prénom

Règles :
- Chaque page appartient à exactement un groupe.
- Les groupes doivent couvrir toutes les pages du document, dans l'ordre, sans trou ni chevauchement.
- Une attestation fait 1 ou 2 pages maximum.
- `start_page` et `end_page` sont les numéros de page tels qu'indiqués dans le texte fourni (1 = première page)."""

DOCUMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "documents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_page": {"type": "integer"},
                    "end_page": {"type": "integer"},
                    "nom": {"type": "string"},
                    "prenom": {"type": "string"},
                },
                "required": ["start_page", "end_page", "nom", "prenom"],
            },
        }
    },
    "required": ["documents"],
}


@dataclass
class ExtractedDocument:
    start_page: int  # 1-indexé, inclus
    end_page: int  # 1-indexé, inclus
    nom: str
    prenom: str


class ProcessingError(Exception):
    """Erreur métier lisible à afficher directement à l'utilisateur."""


def extract_page_texts(pdf_bytes: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [(page.extract_text() or "").strip() for page in reader.pages]


def _build_prompt(page_texts: list[str]) -> str:
    parts = [
        f"--- Page {i} ---\n{text or '(page vide)'}"
        for i, text in enumerate(page_texts, start=1)
    ]
    return "\n\n".join(parts)


def identify_documents(page_texts: list[str], client: genai.Client) -> list[ExtractedDocument]:
    if not page_texts:
        raise ProcessingError("Le PDF ne contient aucune page.")
    if not any(page_texts):
        raise ProcessingError(
            "Aucun texte n'a pu être extrait de ce PDF. "
            "Vérifie qu'il s'agit bien d'un PDF avec texte sélectionnable (pas un scan/image)."
        )

    prompt = _build_prompt(page_texts)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_json_schema=DOCUMENTS_SCHEMA,
        max_output_tokens=16384,
    )

    try:
        response = _generate_with_retries(client, MODEL, prompt, config, RETRY_DELAYS)
    except _ModelUnavailable:
        try:
            response = _generate_with_retries(
                client, FALLBACK_MODEL, prompt, config, FALLBACK_RETRY_DELAYS
            )
        except _ModelUnavailable as fallback_failure:
            raise ProcessingError(
                "Gemini est surchargé, ou le quota gratuit quotidien est atteint "
                "pour les deux modèles disponibles. Réessaie plus tard (le quota "
                "se réinitialise chaque jour)."
            ) from fallback_failure.cause

    text = response.text
    if not text:
        candidate = response.candidates[0] if response.candidates else None
        finish_reason = getattr(candidate, "finish_reason", None)
        raise ProcessingError(
            f"Gemini n'a renvoyé aucune réponse exploitable (raison : {finish_reason}). "
            "Essaie de traiter le PDF en plusieurs lots plus petits."
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProcessingError(f"Réponse de l'IA illisible : {exc}") from exc

    documents = [
        ExtractedDocument(
            start_page=doc["start_page"],
            end_page=doc["end_page"],
            nom=doc["nom"],
            prenom=doc["prenom"],
        )
        for doc in data["documents"]
    ]

    if not documents:
        raise ProcessingError("Aucune attestation n'a été identifiée dans ce PDF.")

    return documents


def _clean_tokens(raw: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", raw or "").encode("ascii", "ignore").decode("ascii")
    return [t for t in re.split(r"[^A-Za-z0-9]+", normalized) if t]


def format_nom(raw: str) -> str:
    tokens = _clean_tokens(raw)
    return "-".join(t.upper() for t in tokens) or "INCONNU"


def format_prenom(raw: str) -> str:
    tokens = _clean_tokens(raw)
    return "-".join(t.capitalize() for t in tokens) or "Inconnu"


@dataclass
class SplitFile:
    nom: str  # déjà normalisé (format_nom)
    prenom: str  # déjà normalisé (format_prenom)
    filename: str
    data: bytes


def _next_filename(used_names: dict[str, int], base_name: str) -> str:
    count = used_names.get(base_name, 0)
    used_names[base_name] = count + 1
    return f"{base_name}.pdf" if count == 0 else f"{base_name}_{count + 1}.pdf"


def split_pdf(
    pdf_bytes: bytes,
    documents: list[ExtractedDocument],
    attestation_type: str,
) -> list[SplitFile]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    label = ATTESTATION_LABELS[attestation_type]
    used_names: dict[str, int] = {}
    results: list[SplitFile] = []

    for doc in documents:
        start = max(1, doc.start_page)
        end = min(total_pages, doc.end_page)
        if start > end:
            continue

        writer = PdfWriter()
        for page_index in range(start - 1, end):
            writer.add_page(reader.pages[page_index])

        nom = format_nom(doc.nom)
        prenom = format_prenom(doc.prenom)
        filename = _next_filename(used_names, f"{nom}_{prenom}_{label}")

        buffer = io.BytesIO()
        writer.write(buffer)
        results.append(SplitFile(nom=nom, prenom=prenom, filename=filename, data=buffer.getvalue()))

    return results


def merge_matching_attestations(
    employeur_files: list[SplitFile],
    of_files: list[SplitFile],
) -> list[tuple[str, bytes]]:
    """Fusionne en un seul fichier les attestations Employeur et OF d'une
    même personne (NOM_Prenom_AttestationsFormation.pdf). Les personnes
    présentes dans un seul des deux jeux de fichiers restent inchangées."""
    of_by_key: dict[tuple[str, str], list[SplitFile]] = {}
    for f in of_files:
        of_by_key.setdefault((f.nom, f.prenom), []).append(f)

    used_names: dict[str, int] = {}
    matched_of_ids: set[int] = set()
    results: list[tuple[str, bytes]] = []

    for emp in employeur_files:
        candidates = of_by_key.get((emp.nom, emp.prenom), [])
        match = next((c for c in candidates if id(c) not in matched_of_ids), None)

        if match is None:
            results.append((emp.filename, emp.data))
            continue

        matched_of_ids.add(id(match))
        writer = PdfWriter()
        for source_bytes in (emp.data, match.data):
            reader = PdfReader(io.BytesIO(source_bytes))
            for page in reader.pages:
                writer.add_page(page)

        filename = _next_filename(used_names, f"{emp.nom}_{emp.prenom}_AttestationsFormation")
        buffer = io.BytesIO()
        writer.write(buffer)
        results.append((filename, buffer.getvalue()))

    # Fichiers OF sans équivalent Employeur : restent individuels.
    for of in of_files:
        if id(of) not in matched_of_ids:
            results.append((of.filename, of.data))

    return results


def process_pdf(pdf_bytes: bytes, attestation_type: str, client: genai.Client) -> list[SplitFile]:
    """Découpe et renomme un PDF de publipostage. `attestation_type` est 'employeur' ou 'of'."""
    page_texts = extract_page_texts(pdf_bytes)
    documents = identify_documents(page_texts, client)
    return split_pdf(pdf_bytes, documents, attestation_type)
