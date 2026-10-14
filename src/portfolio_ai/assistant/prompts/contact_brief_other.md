---
version: 1
source: 'n8n workflow "Portfolio | Contact Form -> Email", node "SET | User Message - Other", the user message'
used_by: contact/reply.py, as the request for a reply to any other message
changes:
  - 'The n8n expressions are named fields, filled in by the code: {email}, {subject}, {message}.'
---

Write a general reply to a contact form message.

Contact form data:
- Email: {email}
- Subject: {subject}
- Message: {message}

Instructions for this reply:
- Thank them for reaching out
- Acknowledge their message
- If the message is unclear, ask them to provide more details about their request
- Keep the reply polite and neutral
