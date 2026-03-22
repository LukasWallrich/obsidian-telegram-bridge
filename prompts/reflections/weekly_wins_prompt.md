You are lightly editing a voice/text reflection into a clean Obsidian note.

IMPORTANT: Preserve the user's own words and voice. Fix grammar issues from voice transcription, add markdown formatting, but do NOT rewrite, summarise, or add your own commentary. This is their reflection, not yours.

## Reflection Context

Chain: Weekly Wins (Friday)
Prompt shown to user: "What have been your greatest wins this week?"

Week-ahead goals (if available):
{context}

## User's Response

{user_response}

## Task

Create a reflection note at: {reflections_folder}/{folder}/{date} Weekly Wins.md

Use this EXACT template:
---
date: {date}
type: reflection
chain: weekly-wins
tags: [reflection, weekly-wins]
---

# Weekly Wins — {date}

> [!note] Prompt
> What have been your greatest wins this week?

{Format the user's response as clean markdown. Use bullet points or paragraphs as appropriate to their style. Fix voice transcription artefacts (filler words, garbled phrases) but keep their language. If week-ahead goals were provided, note any connections — but only if the user themselves referenced them.}

---
*Friday reflection — {date}*

Reply with EXACTLY: SAVED: {date} Weekly Wins.md
