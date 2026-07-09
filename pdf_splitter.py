"""Logique de découpage et de renommage intelligent des PDF de publipostage.

Prend un PDF contenant plusieurs attestations individuelles (issu d'un
publipostage), identifie via Claude les bornes de pages de chaque attestation
ainsi que le nom/prénom de la personne concernée, puis génère un PDF par
personne nommé `NOM_Prenom_AttestationEmployeur.pdf` (ou `..._AttestationOF.pdf`).
"""

from __future__ import annotations

import io
import json
import re
import unicodedata
from dataclasses import dataclass

from anthropic import Anthropic
from pypdf import PdfReader, PdfWriter

MODEL = "claude-opus-4-8"

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
    "type": "json_schema",
    "schema": {
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
                    "additionalProperties": False,
                },
            }
        },
        "required": ["documents"],
        "additionalProperties": False,
    },
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


def identify_documents(page_texts: list[str], client: Anthropic) -> list[ExtractedDocument]:
    if not page_texts:
        raise ProcessingError("Le PDF ne contient aucune page.")
    if not any(page_texts):
        raise ProcessingError(
            "Aucun texte n'a pu être extrait de ce PDF. "
            "Vérifie qu'il s'agit bien d'un PDF avec texte sélectionnable (pas un scan/image)."
        )

    prompt = _build_prompt(page_texts)
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        output_config={"format": DOCUMENTS_SCHEMA},
        messages=[{"role": "user", "content": prompt}],
    )

    if response.stop_reason == "max_tokens":
        raise ProcessingError(
            "La réponse de l'IA a été tronquée (trop de pages en une seule fois). "
            "Essaie de traiter le PDF en plusieurs lots plus petits."
        )
    if response.stop_reason == "refusal":
        raise ProcessingError("Claude a refusé de traiter ce contenu.")

    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    documents = [ExtractedDocument(**doc) for doc in data["documents"]]

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


def split_pdf(
    pdf_bytes: bytes,
    documents: list[ExtractedDocument],
    attestation_type: str,
) -> list[tuple[str, bytes]]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    label = ATTESTATION_LABELS[attestation_type]
    used_names: dict[str, int] = {}
    results: list[tuple[str, bytes]] = []

    for doc in documents:
        start = max(1, doc.start_page)
        end = min(total_pages, doc.end_page)
        if start > end:
            continue

        writer = PdfWriter()
        for page_index in range(start - 1, end):
            writer.add_page(reader.pages[page_index])

        base_name = f"{format_nom(doc.nom)}_{format_prenom(doc.prenom)}_{label}"
        count = used_names.get(base_name, 0)
        used_names[base_name] = count + 1
        filename = f"{base_name}.pdf" if count == 0 else f"{base_name}_{count + 1}.pdf"

        buffer = io.BytesIO()
        writer.write(buffer)
        results.append((filename, buffer.getvalue()))

    return results


def process_pdf(pdf_bytes: bytes, attestation_type: str, client: Anthropic) -> list[tuple[str, bytes]]:
    """Découpe et renomme un PDF de publipostage. `attestation_type` est 'employeur' ou 'of'."""
    page_texts = extract_page_texts(pdf_bytes)
    documents = identify_documents(page_texts, client)
    return split_pdf(pdf_bytes, documents, attestation_type)
