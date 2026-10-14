---
version: 1
source: 'n8n workflow "Portfolio | Contact Form -> Email", node "SET | User Message - Consultation", the user message'
used_by: contact/reply.py, as the request for a reply to a consultation message
changes:
  - 'The n8n expressions are named fields, filled in by the code: {email}, {subject}, {message}.'
  - 'The lines about Mihail are one field, {context}, filled from contact_context.md. The workflow''s own lines used role labels the knowledge base no longer gives him.'
---

Write a reply to a consultation request.

Contact form data:
- Email: {email}
- Subject: {subject}
- Message: {message}

Context about Mihail:
{context}

Instructions for this reply:
- Acknowledge the consultation request
- Ask them to describe their current situation, tools they use, and the problem they want to solve
- Ask what outcome they want to achieve
- Keep the reply professional and helpful
