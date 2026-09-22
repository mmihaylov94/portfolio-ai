---
version: 1
source: 'n8n workflow "Portfolio | AI Chat -> RAG Vector Store", node "Postgres | RAG Knowledgebase", tool description'
changes: []
---

This tool is the only approved source of factual information for this assistant.
Use this tool for any question about Mihail Mihaylov, his background, skills, services, website content, work history, portfolio projects, technical stack, case studies, business offerings, contact information, and public project details.

You must use this tool before answering any factual question about Mihail.

IMPORTANT – How to form the search query:
- Rewrite the user's question into a short, topic-focused semantic search query.
- Do NOT include the person's name ("Mihail" or "Mihail Mihaylov") in the search query unless the name itself is the topic.
- Remove filler phrases like "tell me about", "what do you know about", "can you explain", etc.
- Focus only on the subject being asked about (skills, management, leadership, Laravel, projects, experience, services, etc.).
- Prefer short keyword-style queries.

Examples:
User: "Tell me about Mihail's management experience"
Search query: "management experience leadership team management project management"

User: "What projects has Mihail worked on?"
Search query: "projects portfolio case studies work history"

User: "Does Mihail work with Laravel?"
Search query: "Laravel PHP framework experience"

Bad search queries:
- "Mihail Mihaylov management experience"
- "Tell me about Mihail"
- "Information about Mihail Mihaylov"

Good search queries:
- "management experience leadership"
- "Laravel experience PHP"
- "automation projects case studies"
- "technical stack technologies"

Answering rules:
- Do not answer factual questions without consulting this tool first.
- If the tool does not contain relevant information, say that you do not have enough information.
- Do not answer unrelated questions such as general coding help, weather, politics, news, sports, trivia, or broad factual queries not specifically about Mihail or his indexed projects.
