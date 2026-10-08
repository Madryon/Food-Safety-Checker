---
title: FoodCheck - AI Food Safety Analyzer
emoji: 🍽️
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# FoodCheck: AI Food Safety & Corporate Transparency Checker 🇮🇳

An AI-powered packaged food analyzer built for Indian consumers using Google Gemini AI, Open Food Facts, and real-time news cross-referencing.

## Features
- 📷 Real-time Packaging / Ingredients Label OCR via Gemini Vision
- 📊 Instant Nutrition & Additive Safety Analysis
- ⚠️ Health Flags & Harmful Chemical Alerts (palm oil, trans fats, emulsifiers)
- 🌿 Healthier Indian Market Alternatives
- 📰 Corporate Transparency & Safety Recalls News Feed
- 🥗 **AI Diet Planner (Meta-Llama-3.1-8B-Instruct via Groq Cloud)** — personalized daily meal plans with macros, portion sizes, and hydration schedules
- 🔥 **Real-Time Calorie & Macro Tracker** — daily consumed vs target calorie meter, macronutrient splits, and instant meal logging
- 🗄️ **Aiven PostgreSQL Cloud Storage** — persistent storage for user profiles, diet plans, and meal logs
- 🎨 Multi-Theme Support (Light, Dark, Modern Cyberpunk Techno)
- ⚡ Smart Model Fallback — auto-switches across 5 Gemini models to avoid rate limits
- 🖼️ Image Optimization — resizes & compresses uploads for faster AI processing

## Tech Stack
- **Backend:** Python / Flask / Gunicorn
- **AI Models:** 
  - Google Gemini (multi-model fallback pool for OCR & ingredient safety)
  - Meta-Llama-3.1-8B-Instruct on Groq Cloud (ultra-fast personalized diet planning)
- **Database:** Aiven PostgreSQL (with local SQLite fallback for offline development)
- **Data:** Open Food Facts API, Google News RSS
- **Deploy:** Docker → Render / Hugging Face Spaces

## Environment Variables
Set these in your `.env` or cloud provider environment settings:
- `GEMINI_API_KEY` — Your Google AI Studio API key
- `GNEWS_API_KEY` — (Optional) GNews API key for extended news coverage
- `GROQ_API_KEY` — (Required for AI Diet Plan) Groq Cloud API key for Meta-Llama-3.1
- `GROQ_MODEL` — `llama-3.1-8b-instant` (default)
- `AIVEN_DATABASE_URL` or `DATABASE_URL` — Connection URI for your Aiven PostgreSQL instance (e.g. `postgres://avnadmin:PASSWORD@HOST.aivencloud.com:PORT/defaultdb?sslmode=require`)