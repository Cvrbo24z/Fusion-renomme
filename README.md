# Fusion-renomme
logiciel intelligent de renommage et fusionnage de PDF

## Ce que ça fait

Tu as un PDF issu d'un publipostage (ex : 150 attestations de formation
"travail en hauteur" générées en un seul fichier). Fusion-renomme :

1. Découpe ce PDF en un fichier par personne (1 à 2 pages chacun).
2. Identifie le nom et le prénom de chaque personne via l'IA (Google Gemini).
3. Renomme chaque fichier : `NOM_Prenom_AttestationEmployeur.pdf` ou
   `NOM_Prenom_AttestationOF.pdf` selon le type de document déposé.
4. Te propose de tout télécharger en `.zip`, ou fichier par fichier.

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Il faut une clé API Google Gemini (gratuite, sans carte bancaire) :

1. Va sur https://aistudio.google.com/apikey
2. Connecte-toi avec un compte Google, clique sur "Create API key"
3. Configure-la :

```bash
export GEMINI_API_KEY="ta-clé-ici"
```

## Lancer l'application

```bash
streamlit run app.py
```

Une page s'ouvre dans ton navigateur (par défaut http://localhost:8501).
Dépose ton/tes PDF (Employeur et/ou OF), clique sur "Traiter", puis télécharge
les fichiers renommés.

## Prérequis sur les PDF

- Le texte doit être sélectionnable (PDF natif issu d'un traitement de texte,
  pas un scan/image).
- Chaque attestation individuelle fait 1 ou 2 pages maximum.
- Les attestations Employeur et OF sont dans deux fichiers PDF séparés.
