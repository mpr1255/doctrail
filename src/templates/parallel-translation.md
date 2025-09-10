---
title: "{{ title if title else sha1 }}"
sha1: "{{ sha1 }}"
date: "{{ court_date if court_date else 'undated' }}"
judge: "{{ judge_name if judge_name else 'unknown' }}"
---

{% for idx, zh_line in zh_lines.items() %}
{{ zh_line }}

{{ en_lines[idx] }}

{% endfor %} 