You are lightly editing a voice/text reflection into a clean Obsidian note.

IMPORTANT: Preserve the user's own words and voice. Fix grammar issues from voice transcription, add markdown formatting, but do NOT rewrite, summarise, or add your own commentary. This is their reflection, not yours.

## Reflection Context

Chain: {chain_name}
Prompt shown to user: "{telegram_prompt}"

{context}

## User's Response

{user_response}

## Task

Create a reflection note at: {reflections_folder}/{folder}/{date} {chain_name}.md

Use this EXACT template:
---
date: {date}
type: reflection
chain: {chain_id}
tags: [reflection, {chain_id}]
---

# {chain_name} — {date}

> [!note] Prompt
> {telegram_prompt}

{Format the user's response as clean markdown. Fix voice transcription artefacts but keep their language.}

---
*Reflection — {date}*

Reply with EXACTLY: SAVED: {date} {chain_name}.md
