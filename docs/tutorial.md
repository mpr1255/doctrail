# Tutorial

If you want to understand how to use doctrail, follow these instructions very carefully, on your own computer, in order. It is only by doing this slowly once that the mental model clicks.

What you are about to do: turn a pile of documents into a grid, then add typed columns to that grid.[^column] "Typed" means each new column must take a certain kind of value — a number, a category ("enum"), a short string — and you decide which. You declare your schema (think: codebook) and the model fills the column in.

## Setup

After following the [quickstart](quickstart.md) to install doctrail, open your terminal, navigate to a fresh empty directory, and type:

```bash
doctrail init test
```

This sets up a hidden folder called `.doctrail` (configs live there), a folder called `./data` containing 18 Federalist files — 10 PDFs, 3 HTML files, 5 Word documents — plus UN speech excerpts, and `./out/database.db`, into which those files have already been ingested. The Federalist files include the twelve whose authorship was disputed for 150 years.

Install a database viewer — [TablePlus](https://tableplus.com/) is good — and open `out/database.db`. You will see a `documents` table: one row per file, with the extracted text. Your documents are now in a grid.

Alternatively, open your coding agent in the project folder, give it `https://doctrail.org/llms.txt`, and tell it to get started. That page is the full agent-facing manual in one file: commands, YAML structure, schema examples, and the basic workflow.

## First enrichment

You would like to know things about these files. Who wrote each one — Hamilton, Madison, or Jay? And how hard does the author lean on fear of disunion — measured on a scale from 0 to 5, per a detailed codebook you supply?

We have prepared this so you can test. Examine the file `.doctrail/enrichments/test.yml`. It declares three things: which rows to read, what to ask (the prompt — read it, it is a real codebook), and the shape every answer must take (the schema: an enum for the author, an integer 0–5 for the fear scale, a one-sentence rationale).

Now type:

```bash
doctrail run test
```

This is not really calling a model. We saved the responses ahead of time and the config says `model: replay` — everything else is exactly what a real run does. (When you use your own API key later, the only thing that changes is the model name.)

Open your database again. There is now a `_enrichments` table holding every answer, and a view called `v_documents_enriched`. That view is just a saved SQL query that lays the answers out wide: one row per document, one column per answer. You can change it, or get your agent to make more of them. Sort by `fear_of_disunion`. Check the model's authorship calls against the scholarly consensus column we included. Madison wins.

If you had only wanted a taste, you could have run:

```bash
doctrail run test --limit 5
```

The word `test` here is just the name of the enrichment.

## Second enrichment: your own quantity

Maybe you want to measure some other political or sociological quantity. Define a schema for it and put it in `.doctrail/enrichments/`. We prepared a second one, `securitization.yml`, over a different corpus (`doctrail init test` also gave you `./data/un_speeches/` — excerpts from UN General Debate addresses). It asks a classic question from international relations: does the speaker frame some issue — migration, climate, a pandemic — as an existential threat that justifies extraordinary measures? This schema is more complex: it has a boolean gate, and the issue and intensity fields only mean something when the gate is true. Look at it, then:

```bash
doctrail run securitization --limit 100
```

The schema defines the model, the prompt, and how the model must respond. That "how" is the whole trick: the model is forced to return structured data matching your codebook, every time, and doctrail stores it where SQL can reach it.

## Kicking it up a notch

Two more ideas, because this is where the real power is.

One document, many annotations. A speech mentions many countries. You don't want one answer per speech — you want one answer per country per speech. The `country_mentions.yml` enrichment shows the pattern: the schema returns a list of objects (`mentioned_country`, `stance`), and doctrail explodes them so each mention becomes its own row in the review view.

Multiple models as coders. Anything you measure with one model, you can measure with two and ask how much they agree. We canned responses from two different models:

```bash
doctrail icr country_stance -m replay/coder-a -m replay/coder-b
doctrail icr-report --field stance
```

That prints Krippendorff's alpha and Cohen's kappa — the same intercoder reliability statistics you would report for human coders. If the models can't agree, your codebook is not as clear as you thought, exactly like with research assistants.

To see what the numbers mean, we prepared two more, one at each extreme:

```bash
doctrail icr mentions_climate -m replay/coder-a -m replay/coder-b
doctrail icr-report --field mentions_climate
doctrail icr optimism -m replay/coder-a -m replay/coder-b
doctrail icr-report --field optimism
```

The first asks whether the speech mentions climate change at all. Agreement is near perfect — of course it is: that is a fact of the text, and the codebook says exactly what counts. The second asks how "optimistic" the speech is, 0 to 5, and we wrote that prompt the way people write prompts when they are not thinking: no anchors, no examples, no definition of what a 3 is. Agreement collapses. Now look at why:

```bash
doctrail view pivot icr_optimism -e optimism --by-model
```

Open that view in your database browser and read the rows where the coders differ. The texts are not ambiguous — the codebook is: one coder read "optimism" as tone, the other as concrete commitments, and both readings are defensible because we never said which we meant. The fix is not a better model; it is a better codebook. That loop — code, measure agreement, read the disagreements, tighten the codebook — is the whole discipline, and it now costs minutes instead of a semester of research assistants.

## Now do it on your files

You have now created and run enrichments end to end: ingest, declare a codebook, enrich, inspect the view, scale up, validate.

You are ready to open your own project folder in the terminal (how to use the terminal is another topic — if you can't do that, we cannot help you) and point doctrail at your files.

However, the best way to use doctrail is with a terminal agent, like Claude Code or Codex. OpenAI and Anthropic also produce desktop software that bundles the same functionality. That way you barely need to know how terminals work, or you can skip the terminal entirely and use the GUI: open your agent in the right folder, and tell it to run `doctrail docs` — the full manual prints straight from the CLI, no internet required. Then order it around. A typical way of working: describe the outputs you want; the agent whips up a YAML template; you look at it; you tell the agent to run it with `--limit 10`; you open the database and look at the first `v_` view. If you like what you see, run the lot.

You only need a pile of files you want the text out of, and questions you want answered about each one. Think in terms of the grid: your files are rows, and you are adding typed columns.

One last practical point: the tutorial used replayed responses, so it did not need a provider key. Real enrichments do. Before you swap `model: replay` for OpenAI, Anthropic, Gemini, or OpenRouter, put the relevant key in your project `.env` file, or export it from your shell startup file such as `~/.zshenv` or `~/.bashrc`. A project `.env` is loaded only inside an initialized Doctrail project, and it overrides shell keys; a bare `.env` in an unrelated current directory is ignored. The environment variable names are `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` or `GOOGLE_API_KEY`, and `OPENROUTER_API_KEY`.

[^column]: Note that, under the hood, doctrail does *not* simply add a column to your document grid. It is easy to think of it that way, but this would be the wrong data model. The reason is that your schema may become complicated, and you may want to add lots of enrichments of many forms. Perhaps one document needs multiple sets of the same type of enrichment. For example, it deals with country1, country2, and country3; in that case, your unit of analysis is not a document, but a "country-document", and you have a many-to-one relationship between countries and documents. What doctrail actually does is explained in [the data model](data-model.md).
