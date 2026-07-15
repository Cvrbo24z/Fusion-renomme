"""Application web locale Fusion-renomme.

Lance avec : streamlit run app.py
"""

from __future__ import annotations

import sys

# Certains environnements d'hébergement (conteneurs Linux minimalistes, ex :
# Streamlit Community Cloud) ne configurent pas l'UTF-8 par défaut : les flux
# stdout/stderr héritent alors d'un encodage ASCII, ce qui fait planter tout
# affichage de caractères accentués (noms français, texte des PDF...). On
# force l'UTF-8 explicitement, avant tout autre import, pour éviter ça.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import os
import zipfile
from io import BytesIO

import streamlit as st
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from pdf_splitter import ProcessingError, SplitFile, merge_matching_attestations, process_pdf

st.set_page_config(page_title="CvrboTraining", page_icon="📄")

st.title("📄 CvrboTraining")
st.caption(
    "Divise un PDF de publipostage (ex : attestations de formation) en un fichier "
    "par personne, renommé automatiquement NOM_Prenom_Attestation....pdf"
)

api_key = os.environ.get("GEMINI_API_KEY", "")
if not api_key:
    st.warning(
        "⚠️ La variable d'environnement `GEMINI_API_KEY` n'est pas définie. "
        "Configure-la avant de traiter un PDF (voir README.md)."
    )

st.subheader("1. Dépose tes PDF")
col1, col2 = st.columns(2)
with col1:
    employeur_file = st.file_uploader(
        "Attestations Employeur (un seul PDF, plusieurs personnes)",
        type="pdf",
        key="employeur",
    )
with col2:
    of_file = st.file_uploader(
        "Attestations OF (un seul PDF, plusieurs personnes)",
        type="pdf",
        key="of",
    )

process_clicked = st.button(
    "Traiter",
    type="primary",
    disabled=not (employeur_file or of_file),
)

if process_clicked:
    if not api_key:
        st.error("Impossible de continuer sans GEMINI_API_KEY.")
        st.stop()

    # Par défaut, la librairie Google désactive le délai d'expiration des
    # requêtes HTTP (elle peut rester bloquée indéfiniment si le serveur
    # Gemini est surchargé sans jamais répondre) : on fixe une limite de
    # 3 minutes par tentative — assez pour un gros PDF, mais borné pour
    # éviter tout blocage infini.
    client = genai.Client(
        api_key=api_key,
        http_options=genai_types.HttpOptions(timeout=180_000),
    )
    jobs = []
    if employeur_file is not None:
        jobs.append(("employeur", employeur_file, "Attestations Employeur"))
    if of_file is not None:
        jobs.append(("of", of_file, "Attestations OF"))

    results_by_type: dict[str, list[SplitFile]] = {}

    for attestation_type, uploaded_file, label in jobs:
        with st.spinner(f"Analyse des {label} en cours (cela peut prendre un moment)..."):
            try:
                outputs = process_pdf(uploaded_file.getvalue(), attestation_type, client)
            except ProcessingError as exc:
                st.error(f"{label} : {exc}")
                continue
            except genai_errors.APIError as exc:
                st.error(f"{label} : erreur de l'API Gemini.")
                st.exception(exc)
                continue
            except Exception as exc:  # noqa: BLE001
                st.error(f"{label} : erreur inattendue.")
                st.exception(exc)
                continue

        st.success(f"{label} : {len(outputs)} attestation(s) générée(s).")
        results_by_type[attestation_type] = outputs

    if "employeur" in results_by_type and "of" in results_by_type:
        all_outputs = merge_matching_attestations(
            results_by_type["employeur"], results_by_type["of"]
        )
    else:
        all_outputs = [
            (f.filename, f.data) for files in results_by_type.values() for f in files
        ]

    if all_outputs:
        st.subheader("2. Résultats")

        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename, data in all_outputs:
                zf.writestr(filename, data)

        st.download_button(
            label="⬇️ Télécharger tout en .zip",
            data=zip_buffer.getvalue(),
            file_name="attestations_renommees.zip",
            mime="application/zip",
            type="primary",
        )

        st.divider()
        for filename, data in all_outputs:
            st.download_button(
                label=f"⬇️ {filename}",
                data=data,
                file_name=filename,
                mime="application/pdf",
                key=f"dl-{filename}",
            )
