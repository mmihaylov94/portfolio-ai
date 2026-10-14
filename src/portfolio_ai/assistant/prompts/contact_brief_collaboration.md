---
version: 1
source: 'n8n workflow "Portfolio | Contact Form -> Email", node "SET | User Message - Collaboration", the user message'
used_by: contact/reply.py, as the request for a reply to a collaboration message
changes:
  - 'The n8n expressions are named fields, filled in by the code: {email}, {subject}, {message}.'
  - 'The lines about Mihail are one field, {context}, filled from contact_context.md. The workflow''s own lines used role labels the knowledge base no longer gives him.'
---

Write a reply to a collaboration inquiry.

Contact form data:
- Email: {email}
- Subject: {subject}
- Message: {message}

Context about Mihail:
{context}

Instructions for this reply:
- Thank them for the collaboration idea
- Express interest
- Ask what type of collaboration they have in mind
- Ask what the expected roles and responsibilities would be
- Suggest continuing the conversation by email or a short call
