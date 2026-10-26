{#- One thread page as Markdown (#884): the same page, same messages and
    same status as thread.html, through the same filters (see
    message.md). The activity chip is left out because it is relative
    to now, which no validator can carry. -#}
# {{ (root.subject|clean_subject or '(no subject)')|md_escape }}{% if total_pages > 1 %} (page {{ page }} of {{ total_pages }}){% endif +%}

{% if subsystem_hits %}
- Subsystems: {{ subsystem_hits|map(attribute='name')|join(', ')|md_escape }}
{% set maints = subsystem_hits|subsystem_maintainers %}
{% if maints %}
- Maintainers: {{ maints|map('first')|join(', ')|md_escape }}
{% endif %}
{% endif %}
{% if series_totals and series_totals[0] %}
{% set _patches, _files, _ins, _dels = series_totals %}
- Series: {{ _patches }} patch{{ '' if _patches == 1 else 'es' }}{% if _files %} touching {{ _files }} file{{ '' if _files == 1 else 's' }}{% endif %}{% if _ins is not none %}, +{{ _ins }}/-{{ _dels }}{% endif +%}
{% endif %}
{% if patch_state and patch_state.is_patch and lifecycle_status and lifecycle_status.state.value != 'pending' %}
- Status: {{ lifecycle_status.pill_label|md_escape }}{{ lifecycle_status.count_suffix|md_escape }}{% if lifecycle_status.tooltip %} ({{ lifecycle_status.tooltip|md_escape }}){% endif +%}
{% endif %}
{% set status_sentence = lifecycle_status|status_line if patch_state and patch_state.is_patch else "" %}
{% if status_sentence %}
- {{ status_sentence|md_escape }}
{% endif %}
{% set synthesis = patch_state|patch_synthesis %}
{% if synthesis %}
- {{ synthesis|md_escape }}
{% endif %}
- List: {{ inbox_name|md_escape }}
- {{ total_count }} message{{ '' if total_count == 1 else 's' }}, {{ thread_summary.author_count }} author{{ '' if thread_summary.author_count == 1 else 's' }}
- Page: {{ canonical_url }}
{% if prev_page_url %}
- Previous page: {{ site_base ~ prev_page_url|markdown_path }}
{% endif %}
{% if next_page_url %}
- Next page: {{ site_base ~ next_page_url|markdown_path }}
{% endif %}
{% for node, parsed, url in messages %}

## {{ (node.subject|clean_subject or '(no subject)')|md_escape }}

- From: {{ node.author|safe_from|md_escape }}
- Date: {{ node.date.strftime('%Y-%m-%d %H:%M:%S %z') if node.date else '?' }}
- Message: {{ site_base ~ url|markdown_path }}

{% if parsed and parsed.body %}
{% set text = parsed.body|render_body_text %}
{% set fence = text|md_fence %}
{{ fence }}text
{{ text }}
{{ fence }}
{% elif parsed %}
_(no body)_
{% else %}
_Message body unavailable; try {{ site_base ~ url }}._
{% endif %}
{% endfor %}
