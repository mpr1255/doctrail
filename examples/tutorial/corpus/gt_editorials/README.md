# Global Times editorials (econ-threat demo corpus)

Ten Chinese-language Global Times editorials (社评 / 环球时报社评), used by
`doctrail init test econ-threat` to demonstrate a many-to-one enrichment: one
editorial is coded into several country-editorial rows, one per foreign country
the text targets, each with an economic-threat rating.

These are the source texts only. The baked replay responses
(`.doctrail/replay/econ_threat.jsonl`) — full English translations plus the 0-3
economic-threat ratings per country — were produced once with `gpt-5-mini` and
committed so the demo runs fully offline. Re-keying the fixtures requires
re-hashing the files (the replay key is the sha1 of the file bytes).

Provenance: each editorial was selected from the `words_before_deeds` research
corpus, filtered to the most strongly worded pieces that name multiple
countries. Originals are public editorials from opinion.huanqiu.com.

| file | date | sha1 | source |
| --- | --- | --- | --- |
| gt_2012_philippines.txt | 2012-04-21 | 279279c5 | https://opinion.huanqiu.com/1152/2012-04/2657275.html |
| gt_2013_eu_wine.txt | 2013-06-06 | bdd7af7c | https://opinion.huanqiu.com/article/9CaKrnJANPA |
| gt_2013_north_korea.txt | 2013-01-25 | eec24caa | https://opinion.huanqiu.com/article/9CaKrnJz1yT |
| gt_2016_south_korea_thaad.txt | 2016-07-08 | 5ca450a6 | https://opinion.huanqiu.com/article/9CaKrnJWn0P |
| gt_2017_australia.txt | 2017-09-01 | 241de696 | https://opinion.huanqiu.com/article/9CaKrnK504L |
| gt_2018_canada_meng.txt | 2018-12-08 | f62df56f | https://opinion.huanqiu.com/article/9CaKrnKfImu |
| gt_2018_us_trade_war.txt | 2018-06-23 | 323f9778 | https://opinion.huanqiu.com/article/9CaKrnK9JsJ |
| gt_2021_afghanistan.txt | 2021-08-24 | ed468293 | https://opinion.huanqiu.com/article/44UB8CgXovV |
| gt_2021_europe.txt | 2021-03-24 | 23e64ce4 | https://opinion.huanqiu.com/article/42RB1md2gv8 |
| gt_2021_us_taiwan.txt | 2021-09-12 | d0a4bc0f | https://opinion.huanqiu.com/article/44k3twGoFQh |
