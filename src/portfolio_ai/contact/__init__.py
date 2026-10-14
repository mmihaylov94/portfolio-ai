"""The contact form's flow: check a message, classify it, acknowledge it, pass it on.

The site's form posts to the portfolio's own API, which forwards here
(``api/routers/contact.py``). Until October 2026 this was an n8n workflow; the
modules are that workflow's steps, in its order:

- ``rules``       clean the fields, and score the message for spam with fixed rules
- ``classifier``  ask a model: spam or not, what it is really about, is a reply safe
- ``reply``       ask a model for a short acknowledgement to the visitor
- ``emails``      build the acknowledgement and the owner's notification
- ``replies``     who may be sent an automatic reply, and how many a day in all
- ``service``     the flow that joins them, and what happens when a step fails
- ``desk``        the hourly count, the messages in hand, and the reply count

docs/CONTACT.md walks through it.
"""
