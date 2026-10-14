---
version: 1
source: 'n8n workflow "Portfolio | Contact Form -> Email", node "SET | User Message - Project", the user message'
used_by: contact/reply.py, as the request for a reply to a project message
changes:
  - 'The n8n expressions are named fields, filled in by the code: {email}, {subject}, {message}.'
  - 'The lines about Mihail are one field, {context}, filled from contact_context.md. The workflow''s own lines used role labels the knowledge base no longer gives him.'
---

Write a reply to a project inquiry.

Contact form data:
- Email: {email}
- Subject: {subject}
- Message: {message}

Context about Mihail:
{context}

Instructions for this reply:
- Show interest in the project
- Thank them for reaching out
- Ask for the most important project details: scope, timeline, budget range, and tech stack (if applicable)
- Suggest a call or email discussion as next step
