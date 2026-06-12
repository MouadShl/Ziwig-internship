<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=gradient&customColorList=6,11,20&height=220&section=header&text=Ziwig%20Internship&fontSize=58&fontColor=fff&fontAlignY=38&desc=Data%20Science%20Pipeline%20for%20Endometriosis%20Patient%20Insights&descAlignY=62&descSize=17&descColor=ffffffcc&animation=fadeIn" width="100%"/>

<br/>

[![Typing SVG](https://readme-typing-svg.demolab.com?font=JetBrains+Mono&weight=600&size=17&duration=3000&pause=1000&color=A78BFA&center=true&vCenter=true&width=720&lines=From+raw+web+data+to+strategic+business+intelligence.;Scraping+%E2%86%92+NLP+%E2%86%92+Sentiment+%E2%86%92+Topic+Modeling+%E2%86%92+Data+Warehouse+%E2%86%92+Power+BI;An+end-to-end+pipeline+built+for+Ziwig+Morocco.)](https://git.io/typing-svg)

<br/>

![Status](https://img.shields.io/badge/Status-Completed-22C55E?style=for-the-badge)
![Company](https://img.shields.io/badge/Company-Ziwig%20Morocco-A78BFA?style=for-the-badge)
![Author](https://img.shields.io/badge/Author-Mouad%20Souhal-3B82F6?style=for-the-badge)
![Type](https://img.shields.io/badge/Type-PFE%20%E2%80%93%20End%20of%20Studies%20Project-F59E0B?style=for-the-badge)

</div>

<br/>

---

## 📖 Project Overview

This repository contains the complete **end-to-end data science pipeline** built during my final-year internship (PFE) at **Ziwig Morocco**, under the supervision of **M. Oussama Ettizoui**.

The mission: build a data intelligence system that collects, analyzes, and visualizes patient conversations about **endometriosis** across forums, associations, and community platforms — turning unstructured public discussions into **patient insights** and **market opportunity scoring** for France and international markets.

<div align="center">

| 🎯 Axis 1 — Patient Insights | 🌍 Axis 2 — Market Insights |
|:---|:---|
| Sentiment, themes, symptoms, pain points, and brand perception extracted from real patient conversations. | Country-level activity scoring, opportunity ranking, and service/delivery friction analysis (SAV). |

</div>

<br/>

---

## 🏗️ Pipeline Architecture

<div align="center">

```
┌─────────────┐    ┌──────────────┐    ┌─────────────┐    ┌──────────────┐
│   E1        │    │   E2         │    │   E3        │    │   E4         │
│  Scraping   │ -> │  Cleaning &  │ -> │  NLP Prep & │ -> │  Sentiment    │
│  Forums &   │    │ Normalization│    │  Lang Detect│    │  Analysis     │
│ Associations│    │              │    │             │    │ (Transformers)│
└─────────────┘    └──────────────┘    └─────────────┘    └──────────────┘
                                                                    |
┌─────────────┐    ┌──────────────┐    ┌─────────────┐            v
│   E7        │    │   E6         │    │   E5        │    ┌──────────────┐
│  Power BI   │ <- │  Data        │ <- │  KPI &      │ <- │   Topic       │
│  Dashboards │    │  Warehouse   │    │  Scoring    │    │  Modeling     │
│ (Axe 1 & 2) │    │ (Star Schema)│    │             │    │  (BERTopic)   │
└─────────────┘    └──────────────┘    └─────────────┘    └──────────────┘
```

</div>

<br/>

---

## 🔄 Pipeline Stages (E1 → E7)

<div align="center">

| Stage | Name | Description | Key Tools |
|:---:|:---|:---|:---|
| **E1** | Data Collection | Web scraping across 50+ forums and association websites (FR/EN/multi-lang) | Python, Requests, BeautifulSoup |
| **E2** | Data Cleaning | Normalization, deduplication, text cleaning, structure validation | Pandas, Regex |
| **E3** | NLP Preprocessing | Language detection, tokenization, preprocessing for transformer models | spaCy, langdetect |
| **E4** | Sentiment Analysis | Multilingual sentiment classification on patient conversations | CamemBERT, RoBERTa, XLM-RoBERTa |
| **E5** | KPI & Scoring | Volume, engagement, sentiment KPIs + country opportunity scoring | Pandas, SQL |
| **E6** | Topic Modeling | Unsupervised theme discovery across patient discussions | BERTopic, HDBSCAN, UMAP |
| **E7** | Data Warehouse & BI | Star-schema warehouse + interactive dashboards | SQL Server, Power BI |

</div>

<br/>

---

## 🌐 Data Collection — Scraping at Scale

A **universal, config-driven scraper** was built to handle 50+ heterogeneous websites — forums, news/blog associations, and professional directories — across 10+ languages (FR, EN, DE, NL, IT, ES, etc.).

<div align="center">

![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![BeautifulSoup](https://img.shields.io/badge/BeautifulSoup-43B02A?style=for-the-badge&logo=python&logoColor=white)
![Requests](https://img.shields.io/badge/Requests-000000?style=for-the-badge&logo=python&logoColor=white)

</div>

**Key features of the scraping engine :**

- 🔧 **Config-driven architecture** — each site defined by a JSON config (selectors, sections, pagination, language)
- 🔁 **Resume capability** — automatic checkpointing, no duplicate scraping
- 🌍 **Multi-source support** — forums (threads/replies), associations (news/blog), and professional directories
- 🧹 **Built-in content cleaning** — strips navigation, ads, share buttons, related posts
- 📅 **Multilingual date parsing** — handles FR/EN/multi-format publication dates
- ⏱️ **Polite scraping** — configurable rate limiting, retry logic, robots.txt compliance

<br/>

---

## 🤖 NLP & Machine Learning

<div align="center">

![PyTorch](https://img.shields.io/badge/PyTorch-EE4C2C?style=for-the-badge&logo=pytorch&logoColor=white)
![HuggingFace](https://img.shields.io/badge/HuggingFace-FFD21E?style=for-the-badge&logo=huggingface&logoColor=black)
![spaCy](https://img.shields.io/badge/spaCy-09A3D5?style=for-the-badge&logo=spacy&logoColor=white)
![Scikit-Learn](https://img.shields.io/badge/Scikit--Learn-F7931E?style=for-the-badge&logo=scikit-learn&logoColor=white)

</div>

### Sentiment Analysis
Three transformer-based models were evaluated and applied for multilingual sentiment classification on real patient text:

<div align="center">

![CamemBERT](https://img.shields.io/badge/CamemBERT-FF6F61?style=for-the-badge&logo=huggingface&logoColor=white)
![RoBERTa](https://img.shields.io/badge/RoBERTa-8B5CF6?style=for-the-badge&logo=huggingface&logoColor=white)
![XLM--RoBERTa](https://img.shields.io/badge/XLM--RoBERTa-EC4899?style=for-the-badge&logo=huggingface&logoColor=white)

</div>

### Topic Modeling
Discovering the underlying themes in thousands of patient conversations — symptoms, treatments, diagnosis journeys, quality of life — using unsupervised topic modeling.

<div align="center">

![BERTopic](https://img.shields.io/badge/BERTopic-14B8A6?style=for-the-badge&logoColor=white)
![HDBSCAN](https://img.shields.io/badge/HDBSCAN-6366F1?style=for-the-badge&logoColor=white)
![UMAP](https://img.shields.io/badge/UMAP-A855F7?style=for-the-badge&logoColor=white)

</div>

> 📌 **Note:** The noise cluster (Topic -1) generated by HDBSCAN was explicitly analyzed and documented as part of the project's academic rigor — explaining why a portion of conversations cannot be confidently assigned to a specific theme.

<br/>

---

## 🗄️ Data Warehouse — Star Schema (SQL Server)

A dimensional **star-schema Data Warehouse** was designed to structure cleaned and enriched data for analytics and reporting.

<div align="center">

![SQL Server](https://img.shields.io/badge/SQL%20Server-CC2927?style=for-the-badge&logo=microsoft-sql-server&logoColor=white)
![T-SQL](https://img.shields.io/badge/T--SQL-336791?style=for-the-badge&logo=postgresql&logoColor=white)

</div>

**Architecture:** Raw → Clean → Curated, following a layered medallion-style approach, feeding fact and dimension tables for sentiment, topics, sources, countries, and time.

<br/>

---

## 📊 Business Intelligence — Power BI Dashboards

Two interactive Power BI dashboards were built to translate the pipeline's output into decision-ready insights:

<div align="center">

![Power BI](https://img.shields.io/badge/Power%20BI-F2C811?style=for-the-badge&logo=powerbi&logoColor=black)
![DAX](https://img.shields.io/badge/DAX-F2C811?style=for-the-badge&logo=powerbi&logoColor=black)

</div>

<div align="center">

| Dashboard | Focus |
|:---:|:---|
| **Axe 1 — Patient Insights** | Sentiment trends, top themes, symptoms & treatments, Ziwig brand perception |
| **Axe 2 — Market Insights** | Country activity, opportunity scoring (0–100), SAV/delivery friction analysis |

</div>

<br/>

---

## 📈 KPIs & Opportunity Scoring

<div align="center">

| KPI Category | Examples |
|:---|:---|
| **Volume** | Posts/comments per source and time period |
| **Engagement** | Upvotes, likes, comments, replies, shares |
| **Sentiment** | Global sentiment + Ziwig-specific mention sentiment |
| **Themes** | Top discussion topics (pain, diagnosis journey, surgery, treatment, quality of life) |
| **Symptoms & Treatments** | Most frequently cited terms and trends |
| **Service Friction** | SAV/delivery complaint volumes and verbatims |
| **Ecosystem Signals** | Mentions of associations, collectives, and care centers |

</div>

**Opportunity Scoring Formula:**

```
Country Opportunity Score = (Volume + Growth + Symptom Signal + Sentiment) − Service Friction
```

Output: country ranking into **High Potential / Medium / Watchlist** tiers, each backed by indicator-level justification.

<br/>

---

## 🛠️ Full Technical Stack

<div align="center">

### Languages & Data
![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![SQL](https://img.shields.io/badge/SQL-336791?style=for-the-badge&logo=postgresql&logoColor=white)
![Pandas](https://img.shields.io/badge/Pandas-150458?style=for-the-badge&logo=pandas&logoColor=white)
![NumPy](https://img.shields.io/badge/NumPy-013243?style=for-the-badge&logo=numpy&logoColor=white)

### NLP & Machine Learning
![Transformers](https://img.shields.io/badge/Transformers-FFD21E?style=for-the-badge&logo=huggingface&logoColor=black)
![PyTorch](https://img.shields.io/badge/PyTorch-EE4C2C?style=for-the-badge&logo=pytorch&logoColor=white)
![spaCy](https://img.shields.io/badge/spaCy-09A3D5?style=for-the-badge&logo=spacy&logoColor=white)
![Scikit-Learn](https://img.shields.io/badge/Scikit--Learn-F7931E?style=for-the-badge&logo=scikit-learn&logoColor=white)

### Storage & BI
![SQL Server](https://img.shields.io/badge/SQL%20Server-CC2927?style=for-the-badge&logo=microsoft-sql-server&logoColor=white)
![Power BI](https://img.shields.io/badge/Power%20BI-F2C811?style=for-the-badge&logo=powerbi&logoColor=black)

### Tools
![Git](https://img.shields.io/badge/Git-F05032?style=for-the-badge&logo=git&logoColor=white)
![GitHub](https://img.shields.io/badge/GitHub-181717?style=for-the-badge&logo=github&logoColor=white)
![VS Code](https://img.shields.io/badge/VS%20Code-007ACC?style=for-the-badge&logo=visual-studio-code&logoColor=white)
![Jupyter](https://img.shields.io/badge/Jupyter-F37626?style=for-the-badge&logo=jupyter&logoColor=white)

</div>

<br/>

---

## 📁 Repository Structure

```
Ziwig-internship/
│
├── scrapers/                  # Universal scraping engine (forums + associations)
│   ├── scraper_association_universal.py
│   ├── scraper_bs4_universal.py
│   └── scraper_request_*.py   # Source-specific scrapers
│
├── configs/                    # JSON configs per source (selectors, sections, pagination)
│
├── Cleaning/                    # E2 — Data cleaning & normalization
│
├── E3/                          # NLP preprocessing & language detection
├── E4/                          # Sentiment analysis (CamemBERT, RoBERTa, XLM-RoBERTa)
├── E5/                          # KPI computation & opportunity scoring
├── E6/                          # Topic modeling (BERTopic, HDBSCAN, UMAP)
├── E7/                          # Data Warehouse schema + Power BI dashboards
│
├── report/                      # Final PFE report, slides, and figures
│
└── README.md
```

<br/>

---

## 📋 Key Learnings & Engineering Practices

<div align="center">

| Practice | Why it matters |
|:---|:---|
| ✅ **Precise, real figures throughout** | Every reported number (message counts, %, bugs) is traceable and verifiable |
| ✅ **Bug documentation with root cause + fix** | Demonstrates rigorous debugging and reproducibility |
| ✅ **Volume drop justification between stages** | Each pipeline stage's data loss is explicitly explained, not hidden |
| ✅ **Topic -1 noise cluster explained** | HDBSCAN artifacts documented for academic transparency |
| ✅ **Anticipated jury questions (Annexe)** | Proactive technical defense documentation |

</div>

<br/>

---

## 🎓 About This Project

<div align="center">

**Internship (PFE — Projet de Fin d'Études)**
**Ziwig Morocco** · Supervised by **M. Oussama Ettizoui**
**Data Science Engineering — Sup MTI Rabat**

<br/>

[![Email](https://img.shields.io/badge/Email-EA4335?style=for-the-badge&logo=gmail&logoColor=white)](mailto:msouhal.dev@gmail.com)
[![GitHub](https://img.shields.io/badge/GitHub-181717?style=for-the-badge&logo=github&logoColor=white)](https://github.com/MouadShl)
[![Kaggle](https://img.shields.io/badge/Kaggle-20BEFF?style=for-the-badge&logo=kaggle&logoColor=white)](https://www.kaggle.com/souhalmouad)

<br/>

<img src="https://capsule-render.vercel.app/api?type=waving&color=gradient&customColorList=6,11,20&height=120&section=footer" width="100%"/>

</div>
