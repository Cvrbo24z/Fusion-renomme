"""Application web locale Fusion-renomme.

Lance avec : streamlit run app.py
"""

from __future__ import annotations

import os
import zipfile
from io import BytesIO

import streamlit as st
from anthropic import Anthropic, AnthropicError

from pdf_splitter import ProcessingError, process_pdf

st.set_page_config(page_title="Fusion-renomme", page_icon="📄")

st.title("📄 Fusion-renomme")
st.caption(
    "Divise un PDF de publipostage (ex : attestations de formation) en un fichier "
    "par personne, renommé automatiquement NOM_Prenom_Attestation....pdf"
)

api_key = os.environ.get("ANTHROPIC_API_KEY", "")
if not api_key:
    st.warning(
        "⚠️ La variable d'environnement `ANTHROPIC_API_KEY` n'est pas définie. "
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
        st.error("Impossible de continuer sans ANTHROPIC_API_KEY.")
        st.stop()

    client = Anthropic()
    jobs = []
    if employeur_file is not None:
        jobs.append(("employeur", employeur_file, "Attestations Employeur"))
    if of_file is not None:
        jobs.append(("of", of_file, "Attestations OF"))

    all_outputs: list[tuple[str, bytes]] = []

    for attestation_type, uploaded_file, label in jobs:
        with st.spinner(f"Analyse des {label} en cours (cela peut prendre un moment)..."):
            try:
                outputs = process_pdf(uploaded_file.getvalue(), attestation_type, client)
            except ProcessingError as exc:
                st.error(f"{label} : {exc}")
                continue
            except AnthropicError as exc:
                st.error(f"{label} : erreur de l'API Claude ({exc}).")
                continue
            except Exception as exc:  # noqa: BLE001
                st.error(f"{label} : erreur inattendue ({exc}).")
                continue

        st.success(f"{label} : {len(outputs)} attestation(s) générée(s).")
        all_outputs.extend(outputs)

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
