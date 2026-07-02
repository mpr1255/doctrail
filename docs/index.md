# Doctrail

<div class="dt-start-callout" markdown>
To begin right away, point your agent at [doctrail.org/llms.txt](https://doctrail.org/llms.txt) and/or run `uvx doctrail`.
</div>

Doctrail is a software library that allows researchers to perform and validate the large-scale enrichment of text corpora with large language models. It is written to be driven by agents (Claude Code, Codex) as much as humans, though humans must understand how it works. It grew naturally out of several applied computational social science research projects, eventually evolving to become a standalone tool.

Here is an example.

![Ingest a folder of documents, inspect the YAML codebook, enrich, and query the results — all in the terminal](assets/demo.gif)


## What did I just watch?

Let's step through it.

First, we ingest a pile of documents (pdf, doc, docx, xlsx, html; doctrail handles ~a dozen file types) that are in a folder. This is your corpus. "Ingest" here means that we pull the text out of the file and put it into a database that is on your computer.

Second, we looked at what they were (indeed, html, docx, and pdf files).

Third — this is the most important part — we looked at a set of instructions to an LLM (called a 'prompt') and a codebook that defined:

- What is going *into* the LLM (one row from our Federalist papers database each time, plus our prompt explaining what we want *out*)
- The prompt said "Code this... using the codebook below." The model is then instructed to identify the author and to measure the "fear of disunion" the text shows, including what those measures mean.
- What must come *out* of the LLM. This is a schema that enforces only certain responses. When we ask for the coding of a text for social science, we do not want ChatGPT to say "My, what a brilliant question..." We want only the outputs, and *types* of output, that we define. Here, that is defined by the schema (i.e. codebook), in this case like:

`author: {enum: ["Hamilton", "Madison", "Jay"]}`

This means that we want a field called "author" and the only values it can take are one of those three (an enum, or categorical variable).

Similarly with the field "fear of disunion":

`fear_of_disunion: {type: integer, minimum: 0, maximum: 5}`

This can *only* emit the type integer that takes the values between 0 and 5.

Fourth, we examined the output with a SQL command.

The rest of this page explains more about how Doctrail works, the mental model that is most helpful for working with it, and gives two examples of real social science projects that Doctrail facilitated.

## Your corpus as a grid

Doctrail turns a pile of documents — your corpus — into a table, or a grid. So, you should think about your workflow with it as often the equivalent to "adding columns to my documents, which is now a grid."

First, it creates an SQLite database. SQLite is the most widely used database in the world. It exists as a single file on disk and can be easily shared, copied, or backed up. It can be opened in any database browser, and in fact Doctrail is best when you are toggling between *looking* at your LLM enrichments in one window, updating your prompt in another, and re-running your enrichment in a third. Increasingly however, much of this can be subsumed, with the right instructions, by a terminal agent (again, like Claude and Codex).

It is very important that we use a database for all this.

## Why a database, and what's in it?

At a high level, a relational database consists of tables that can be linked to each other with keys.

In doctrail, your files get turned into text and ingested into a table called `documents`.

Doctrail's tables are prefixed by `_`, so they cluster together and stay out of the way.

As Doctrail uses LLMs to enrich the files, the results are stored in an append-only log. SQL queries are then used to reconstruct pieces of these into other tables, or views, that you can inspect and do useful work on. The internal machinery is complex, and many thousands of lines of code define the behaviour. The key idea is that every input to the LLM, and every output from the LLM, is always captured in the database and fully auditable. This means it can be reconstructed in arbitrary ways as discussed below.

In the end, all this is intended to make it trivial to iterate on a prompt and codebook, to confirm its behavior on a new random sample, and only then implement it on thousands, tens of thousands, of hundreds of thousands of documents in the corpus.

There are many ancillary benefits to using a database as the storage engine, including:

* Your corpus and its enrichments stay together in a single file, linked by keys;
* Each write is atomic and incremental, meaning you can resume large runs that get interrupted and no data should be lost or corrupted;
* The corpus is never loaded into computer memory at once. This is not a problem for small corpora, but if it grows to hundreds of thousands of documents or millions of documents, it is awkward, inefficient, and sometimes impossible to store all this in memory and repeatedly rewrite it all to disk;
* You can keep the database open as writes are happening and inspect the enrichments directly as they come in;
* You can easily filter your documents and inspect their enrichments;
* All the standard database guarantees — types, keys, and unique constraints that keep the data consistent;
* SQLite is portable and can be read by numerous other types of software.

## Typical workflows

Here are two main ways that doctrail can be used.

**The qualitative triage loop**. I have 3,000 court decisions, and I have a hunch that some much smaller number of them contain data relevant to my research question. I might want to code these with variables, or I may simply want to closely read and think about what is in them and the differences between them. One could use keywords to filter down the number, but it is difficult to think of every possible keyword that could define the research question. One wishes to employ human-like 'understanding' of meaning and to screen each of them individually, looking at the court decision and the research question and saying: "Is this relevant?" In doctrail this would be a cheap screening enrichment (`relevant: boolean`, i.e. the column name would be `relevant` and it could take a value of `0` or `1`), run in batch mode on a cheap model, and it would result in a few hundred candidate cases for closer analysis. It is then simple to pull random samples from the excluded group to ensure that relevant documents were not left on the table. Not all of the remaining candidate documents may be relevant, but Doctrail has quickly triaged the relevant document set.

**A measurement pipeline.** Many times, one will wish to construct a qualitative measure and apply it identically to every row, producing a measure of some feature of a text that is theoretically relevant. Such a measure might only apply to a subset of documents in a large corpus, so Doctrail can perform the first task of reducing the population of relevant records with a SQL filter, and send only that subset off for enrichment. Whether that measure can be trusted is the subject of the next section.

## Validation

The qualitative coding of some feature in a document is a claim; one will often want to know if such claims are credible. Setting aside the question of truth, the two questions one will ask about any measure are: is it reliable? is it valid?

By **reliability**, we simply want to know whether different coders roughly converge on the same claims. If coders have low agreement about how some feature should be coded, you may have to rethink your measure. `doctrail icr` codes a random sample under several coders, and `doctrail icr-report` scores their agreement (Krippendorff's alpha, Cohen's kappa). Thus, doctrail allows you to randomly sample from your corpus, code such samples with several LLMs (and humans, for that matter), and test the reliability of the measure before running it across the full corpus.

Human coders are stored like LLM coders in the ledger -- both are simply a coder identity. This means one can pool them and test agreement with the same command. To get human codings in, `doctrail overrides-export` writes a CSV template for a run (open it in Excel or anything), a human codes or corrects the rows, and `doctrail overrides-import` reads it back; the human then sits in the ledger as just another coder.

**Validity** is accuracy against a trusted standard. Because a human coder is just another coder in the comparison, the same `doctrail icr-report` gives you this for free: its pairwise table reports how closely each model agrees with the human, so the human-versus-model row is your validity measure. When you would rather eyeball cases than read a statistic, `doctrail review` opens a web UI that walks a human through the model's codings and shows a running accuracy.

These two affordances allow one to validate a codebook on a small random sample, read the disagreements, revise, and only scale once the LLM is behaving.

Doctrail's validation framework is in active development. A key idea is that doctrail *itself* is not intended to be your validation software. It is the canonical store of codings, and provides affordances for getting values in and out, but the statistics one creates will often need to be tailored closely to a specific project, and Doctrail facilitates getting your codes into a rectangle so you can do that.

## Two example use cases

One project began with over 100,000 rows scraped from the Chinese internet. These include media reports, government press releases, announcements on the websites of hospitals, and more. The research question involved identifying the subset of documents that described details of a specific policy, and then measuring the implementation of that policy in a systematic manner. First, Doctrail used a small LLM to run a 'relevance' filter on the documents; the prompt simply described the research question and said "Is this document relevant?" This removed the majority of the corpus. Of course, we then sampled from the 'irrelevant' set to make sure they were indeed irrelevant. Now, on a defined subset, it was possible and meaningful to apply successive enrichments that extracted structured data such as: `policy_name`, `year_began`, `fund_name`, `amount`, `families_involved`, and so forth. This is a far better approach than trying to define a dictionary upfront. And because everything is in SQLite, it became simple to increasingly refine the funnel, so that in the end only a few dozen documents with highly diagnostic evidence were the subject of analysis.

Another project combined tens of thousands of editorials from three PRC state media, in both English and Chinese. First, the table of editorials had to be turned into a table of country-editorial pairs, because this was the unit of analysis. SQLite made this simple and kept all our data together, linked by keys. We could then run successive enrichments across this reshaped table, before finally validating them against human codes. The codes stored in SQLite then fed directly into a build pipeline that performed the modeling and produced the descriptive statistics, tables, and validation measures—meaning that later changes in the database are automatically carried through all outputs.

## Other features

1. **Cache-friendly by default**. As long as the codebook is written with row-specific `{placeholder}` text at the end, most commercial model providers will give a large discount to the cached tokens, significantly reducing the inference costs;
2. **Batch mode.** `doctrail enrich <name> --execution-mode batch` submits through the providers' batch APIs (OpenAI, Anthropic, Gemini) at roughly half the regular price. Large runs are sharded into provider jobs, `doctrail batch watch` follows progress, results reconcile into the same ledger, and partially failed shards simply retry on the next append-mode run;
3. **Packed screening.** For rare-hit boolean screens over short texts, `pack_size` groups many rows into one call and `pack_response_mode: selected_indexes` has the model return only the indexes of the hits — so the 99% of rows that don't match cost almost no output tokens. This can significantly reduce costs for cheap screens;
4. **Cost guardrails.** Before a run, Doctrail estimates the spend and asks you to confirm once it crosses a threshold (default $5), so a misconfigured run cannot use all your money while you sleep; `--skip-cost-check` bypasses it and `--cost-threshold` moves the line;
5. **Model-agnostic.** OpenAI, Anthropic, and Gemini are built in, and OpenRouter is wired in too, so an enrichment can point at any of hundreds of models by name. You can instruct your agent to get Doctrail to list all available models on OpenRouter;
6. **Run diffing.** `doctrail diff-runs` shows precisely where two runs disagree — prompt v1 against v2, or one model against another — so you can see what a codebook change actually moved, then diagnose hard cases;
7. **Ingest from Zotero.** Besides a folder of files (~a dozen formats), `doctrail ingest --zotero` pulls a Zotero library or collection straight into the corpus, so your reference manager can be the source. You have to set this up first.

## Where to go

Use the [quick start](quickstart.md) to install and get going, the [tutorial](tutorial.md) for the guided walkthrough, the [code books](yaml.md) page for the complete config surface, and the [reference](cli.md) for exact commands and flags.

Or, better yet, don't do any of that! Just point your agent (i.e. Codex, Claude Code, whether terminal-based or desktop application) at [llms.txt](https://doctrail.org/llms.txt) and tell it you want to enrich a pile of documents.

As long as you, the human operator, have a fairly clear mental model of how the machinery works, you don't have to manage the implementation details. You describe your goals to the agent, inspect and iterate on the codebook, and get the agent to use Doctrail to carry it out. Doctrail is designed to be driven by agents.

While enrichments are happening, you can open the SQLite database in a software like [TablePlus](https://tableplus.com/) to inspect them and then iterate.
