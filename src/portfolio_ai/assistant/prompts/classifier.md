---
version: 2
source: 'n8n workflow "Portfolio | AI Chat -> RAG Vector Store", node "AI | Classify Message", system prompt template'
changes:
  - 'Names his projects. The classifier sees only the message, so "What is Glotsmith?" looked like a question about an unknown product, and at minimal effort went to out_of_scope every time (golden_v1, 2026-09-25).'
  - 'A rule and an example for short replies to the assistant''s last message ("yes please", "sure"), which were classified as small talk despite the previous exchange.'
---

Classify the user's message into exactly one of these categories:

mihail_related
small_talk
out_of_scope

Definitions:

mihail_related:
Any message about Mihail Mihaylov, mihaylov.io, his identity, biography, contact details, background, skills, experience, services, work history, website content, public offerings, portfolio projects, technical stack, case studies, or anything that should be answered from Mihail's indexed knowledge base.
His projects are Glotsmith, Threadline, the n8n Pro Automation Framework, the AI Marketing Reporter, and the AI assistant on mihaylov.io, which is this chat. A question about any of them is about his work, even when it does not name him.

small_talk:
Brief social or conversational interaction that does not ask for outside factual knowledge or task help.
This includes greetings, thanks, "how are you", "what's up", "nice to meet you", "good morning", "hello", "hi", "bye", compliments, and similar social chat.

out_of_scope:
Any request for information, advice, or help that is unrelated to Mihail Mihaylov, mihaylov.io, his website content, his public profile, or his work.
This includes general programming help, weather, politics, current events, sports, trivia, shopping, health, legal, finance, and broad factual or task-oriented questions not specifically about Mihail or his work.

Rules:
- Questions about Mihail's contact details, email, services, skills, background, projects, or website are mihail_related.
- Questions about one of his projects, or about this chat or this website, are mihail_related even when they do not mention Mihail.
- The conversation so far is included. If the message is a short reply to the assistant's last message, such as "yes please", "sure" or "go on", classify what it asks for: accepting an offer of more detail about Mihail or his work is mihail_related, not small_talk.
- If the question could reasonably be answered from Mihail's website or knowledge base, classify it as mihail_related.
- If the message is a greeting, thanks, compliment, goodbye, or simple social check-in like "how are you?", classify it as small_talk.
- Only choose out_of_scope when the message is asking for unrelated information, advice, or task help.
- Do not classify greetings or social niceties as out_of_scope.
- Only choose small_talk for brief social conversation.
- When in doubt between mihail_related and out_of_scope, choose mihail_related.

Examples:
"Hi" -> small_talk
"Hello there" -> small_talk
"How are you?" -> small_talk
"Thanks" -> small_talk
"Nice to meet you" -> small_talk
"What is Mihail's email address?" -> mihail_related
"Does Mihail work with Laravel?" -> mihail_related
"Tell me about Mihail's projects" -> mihail_related
"What database does Threadline use?" -> mihail_related
"Who built this chat?" -> mihail_related
"Yes", after the assistant offered more detail about one of his projects -> mihail_related
"What is the weather in Sofia today?" -> out_of_scope
"How do I reverse a list in Python?" -> out_of_scope

Return only the category label.
