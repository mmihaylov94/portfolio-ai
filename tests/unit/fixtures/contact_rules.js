// Makes contact_rules.json: what n8n's own code says about a set of invented
// contact form submissions.
//
// src/portfolio_ai/contact/rules.py is a port of the "Code | Validate Request" node
// of the n8n workflow the contact form used until October 2026. This runs that
// node's JavaScript, unchanged, over the submissions below and writes down its
// verdict for each. tests/unit/test_contact_rules.py then holds the Python port to
// every one of them.
//
// The workflow's export is not in the repository (n8n_workflows/ is gitignored: it
// carries instance and credential ids). Run this from the repository root, on a
// machine that has it, when the submissions below change:
//
//     node tests/unit/fixtures/contact_rules.js
//
// The fixture is written with every character outside ASCII as a \u escape, so that
// the zero-width and odd-space characters these cases are about can be seen in a
// diff. The submissions here are written the same way, for the same reason.

const fs = require("node:fs");
const path = require("node:path");

const EXPORT = path.join("n8n_workflows", "Portfolio _ Contact Form -_ Email.json");
const OUT = path.join(__dirname, "contact_rules.json");

const ok = { email: "visitor@example.com", subject: "A new project", reason: "project" };
const LONG = "I would like to talk about a project for my company next month.";
const EMOJI = "\u{1F600}";

// Each: a name for the test's output, and the four fields as the API would hand
// them to the rules. Anything left out is taken from `ok`, with LONG as the message.
const SUBMISSIONS = [
	["plain", {}],

	// --- cleaning -----------------------------------------------------------------
	["tabs-and-line-breaks", { message: "I would like\tto talk\r\nabout a project\n\nfor my company." }],
	["no-break-and-odd-spaces", { message: "I would\u00a0like to\u2003talk about\u2028a project\u3000today." }],
	["next-line-is-not-space", { message: "I would like to talk\u0085about a project today." }],
	["unit-separator-is-not-space", { message: "I would like to talk\u001fabout a project today." }],
	["zero-width-inside-a-keyword", { message: "We offer s\u200be\u200co services for your website today." }],
	["byte-order-mark-inside", { subject: "A\ufeff new\ufeff project" }],
	["html-is-removed", { subject: "<b>A new</b> project", message: "<p>I would like to talk</p><script>alert(1)</script> about a project." }],
	["a-less-than-eats-to-the-next-greater-than", { message: "If a < b and c > d then we should talk about the project." }],
	["an-unclosed-tag-stays", { message: "The budget is < 5000 and the deadline is next month, roughly." }],
	["email-is-lowercased", { email: "  Visitor@Example.COM " }],
	["a-dotted-capital-i", { email: "\u0130NFO@EXAMPLE.COM" }],
	["a-final-sigma", { subject: "\u039f\u0394\u039f\u03a3", message: "\u039f\u0394\u039f\u03a3" }],
	["reason-is-cleaned", { reason: "  Project\n" }],

	// --- validity -------------------------------------------------------------------
	["no-email", { email: "" }],
	["a-message-of-spaces", { message: " \t\u00a0\n" }],
	["a-subject-of-zero-width", { subject: "\u200b\u200d\ufeff" }],
	["a-message-of-a-tag", { message: "<br>" }],
	["an-unknown-reason", { reason: "sales" }],
	["no-reason", { reason: "" }],
	["missing-comes-before-the-reason", { email: "", reason: "sales" }],
	["the-reason-comes-before-the-address", { email: "not-an-address", reason: "sales" }],
	["no-at", { email: "visitor.example.com" }],
	["two-ats", { email: "visitor@@example.com" }],
	["two-addresses", { email: "a@example.com,b@example.com" }],
	["a-space-in-it", { email: "vis itor@example.com" }],
	["no-dot-after-the-at", { email: "visitor@example" }],
	["a-comma-before-the-at", { email: "a,b@example.com" }],
	["a-line-break-at-the-end", { email: "visitor@example.com\n" }],
	["a-line-break-inside", { email: "visitor@example.com\nBcc: other@example.com" }],
	["not-ascii", { email: "b\u00fcro@ex\u00e4mple.com" }],

	// --- the score, rule by rule ------------------------------------------------------
	["a-disposable-domain", { email: "someone@mailinator.com" }],
	["a-subdomain-of-one-is-not", { email: "someone@mail.mailinator.com" }],
	["nine-characters", { message: "Call me!!" }],
	["ten-characters", { message: "Call me!!!" }],
	["four-emoji-are-eight", { message: EMOJI.repeat(4) }],
	["five-emoji-are-ten", { message: EMOJI.repeat(5) }],
	["a-subject-of-one-letter", { subject: "A" }],
	["a-subject-of-one-emoji", { subject: EMOJI }],
	["two-links", { message: "See https://example.com/a and http://example.com/b for the details of it." }],
	["three-links", { message: "See https://example.com/a and http://example.com/b and www.example.com for it." }],
	["three-links-with-a-long-s-are-none", { message: "See http\u017f://example.com/a http\u017f://example.com/b http\u017f://example.com/c for the details." }],
	["three-links-in-capitals", { message: "HTTPS://EXAMPLE.COM/A HTTP://EXAMPLE.COM/B WWW.EXAMPLE.COM are the pages for it." }],
	["two-words-four-times-each", { message: "buy cheap buy cheap buy cheap buy cheap" }],
	["two-words-three-times-each", { message: "buy cheap buy cheap buy cheap now please" }],
	["one-word-four-times", { message: "really really really really want to talk soon" }],
	["five-words-is-too-few", { message: "buy buy buy buy cheap" }],
	["repetition-ignores-case", { message: "Buy CHEAP buy cheap BUY Cheap buy cheap now" }],
	["ten-symbols", { message: "Our prices are great " + "#".repeat(10) + " and we should talk." }],
	["eleven-symbols", { message: "Our prices are great " + "#".repeat(11) + " and we should talk." }],
	["five-emoji-count-as-ten-symbols", { message: "Thank you for the article " + EMOJI.repeat(5) + " it helped." }],
	["six-emoji-count-as-twelve-symbols", { message: "Thank you for the article " + EMOJI.repeat(6) + " it helped." }],
	["letters-outside-ascii-are-symbols", { message: "\u0417\u0434\u0440\u0430\u0432\u0435\u0439\u0442\u0435, \u0438\u043c\u0430\u043c \u043f\u0440\u043e\u0435\u043a\u0442." }],
	["accents-are-symbols", { message: "D\u00e9j\u00e0 vu \u00e0 Z\u00fcrich, t\u00e9l\u00e9phonez \u00e0 l'h\u00f4tel apr\u00e8s d\u00eener s'il vous pla\u00eet." }],
	["seven-capitals-in-ten", { message: "ABCDEFG abc!" }],
	["six-capitals-in-ten", { message: "ABCDEF abcd!" }],
	["nine-capitals-are-too-few", { message: "ABCDEFGHI 123456" }],
	["shouting", { message: "I NEED A WEBSITE BUILT VERY QUICKLY PLEASE" }],
	["subject-is-the-message", { subject: "Please call me back", message: "please CALL me back" }],

	// --- keywords -----------------------------------------------------------------------
	["a-keyword", { message: "We offer backlinks for your website at a good price." }],
	["a-keyword-in-the-subject", { subject: "Guest post offer", message: "I would like to write for your website sometime." }],
	["seo-inside-a-word", { message: "I am writing from Seoul about a museology project." }],
	["a-doctor", { message: "Please pass this message on to Dr Smith at your office." }],
	["da-as-a-word", { message: "Your site has a DA of forty, which we could improve." }],
	["da-inside-a-word", { message: "I saw the data and the dashboard you built last year." }],
	["da-beside-cyrillic", { message: "\u043f\u0440da is how the report ended, oddly enough for us." }],
	["a-long-s-is-not-an-s", { message: "We offer \u017feo services for your website this month." }],
	["a-kelvin-sign-is-not-a-k", { message: "We offer bac\u212alinks for your website this month." }],
	["crypto-in-cryptography", { message: "I teach cryptography and would like to collaborate with you." }],
	["two-keywords", { message: "Casino and betting partners wanted for a new site." }],
	["three-keywords", { message: "Casino and betting and forex partners wanted for a new site." }],
	["a-guarantee", { message: "We give a 100% guarantee on every site that we build." }],
	["a-job-about-traffic", { reason: "job", message: "We have a role open on our web traffic team in Sofia." }],
	["a-project-about-traffic", { reason: "project", message: "We want a dashboard for our web traffic team in Sofia." }],
	["a-job-about-seo", { reason: "job", message: "We are hiring an seo specialist for our team in Sofia." }],

	// --- adding up ------------------------------------------------------------------------
	["two-is-neither", { message: "Call me!!" }],
	["three-is-suspicious", { subject: "A", message: "Call me!!" }],
	["four-is-suspicious", { message: "seo??" }],
	["five-is-spam", { email: "someone@mailinator.com", subject: "A", message: "Call me!!" }],
	["six-is-spam", { email: "someone@mailinator.com", message: "seo??" }],
	["everything-at-once", {
		email: "someone@tempmail.com",
		subject: "BUY NOW",
		message: "BUY NOW casino betting forex https://a.example https://b.example https://c.example BUY NOW BUY NOW BUY NOW",
		reason: "job",
	}],
];

const workflow = JSON.parse(fs.readFileSync(EXPORT, "utf8"));
const node = workflow.nodes.find((candidate) => candidate.name === "Code | Validate Request");
if (!node) throw new Error(`no "Code | Validate Request" node in ${EXPORT}`);
// The node's code reads $json and returns a list of items, as n8n runs it.
const validate = new Function("$json", node.parameters.jsCode);

const cases = SUBMISSIONS.map(([name, fields]) => {
	const input = { ...ok, message: LONG, ...fields };
	const { body, validation } = validate({ body: input })[0].json;
	return {
		name,
		input,
		expected: {
			email: body.email,
			subject: body.subject,
			message: body.message,
			reason: body.reason,
			...validation,
		},
	};
});

// Every character outside printable ASCII as an escape: JSON.stringify leaves most of
// them as they are, and an invisible character in a fixture is one nobody can review.
const text = JSON.stringify(cases, null, "\t").replace(
	/[^\x20-\x7e\n\t]/g,
	(char) => "\\u" + char.charCodeAt(0).toString(16).padStart(4, "0"),
);
fs.writeFileSync(OUT, text + "\n");
console.log(`${cases.length} cases written to ${OUT}`);
