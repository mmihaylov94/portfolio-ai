---
version: 1
source: 'n8n workflow "Portfolio | Contact Form -> Email", node "SET | User Message - Job", the user message'
used_by: contact/reply.py, as the request for a reply to a job message
changes:
  - 'The n8n expressions are named fields, filled in by the code: {email}, {subject}, {message}.'
  - 'The lines about Mihail are one field, {context}, filled from contact_context.md. The workflow''s own lines used role labels the knowledge base no longer gives him.'
---

Write a reply to a job opportunity inquiry.

Contact form data:
- Email: {email}
- Subject: {subject}
- Message: {message}

Context about Mihail:
{context}

Instructions for this reply:
- Thank them for reaching out about the opportunity
- Express interest in learning more
- Ask for key details: role description, tech stack, team structure, remote/on-site, and hiring process
- Keep the tone professional
