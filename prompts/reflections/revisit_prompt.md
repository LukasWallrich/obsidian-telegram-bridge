You are lightly editing a voice/text reflection into a clean Obsidian note.

IMPORTANT: Preserve the user's own words and voice. Fix grammar issues from voice transcription, add markdown formatting, but do NOT rewrite, summarise, or add your own commentary. This is their reflection, not yours.

## Reflection Context

Chain: Note Revisit
Original note: [[{revisit_note_title}]]
Prompt shown to user: "Revisit a saved note and reflect on how your thinking has evolved."

Original note's key takeaways:
{context}

## User's Response

{user_response}

## Task

Create a reflection note at: {reflections_folder}/{folder}/{date} Revisit {short_title}.md

Use this EXACT template:
---
date: {date}
type: reflection
chain: note-revisit
revisited_note: "[[{revisit_note_title}]]"
tags: [reflection, revisit]
---

# Revisit: {short_title} — {date}

> [!note] Revisiting
> [[{revisit_note_title}]]

{Format the user's response as clean markdown. If the user referenced specific notes or concepts, link them using [[Note Title]] wikilinks. Fix voice transcription artefacts but keep their language. Organise their thoughts under subheadings if they covered multiple distinct points — but only if that genuinely helps readability.}

---
*Revisit reflection — {date}*

Reply with EXACTLY: SAVED: {date} Revisit {short_title}.md
