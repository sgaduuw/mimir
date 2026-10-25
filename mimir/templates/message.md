{#- The message page as Markdown (#884), for agents and tools. Mail is
    text, so the body is the mail verbatim in a fence, through the same
    filters as the HTML page: `safe_from` for the sender and
    `render_body_text` for the body. Not autoescaped (by extension), so
    every sender-controlled value outside the fence goes through
    `md_escape`. The Message-ID is left out by design, as on the page. -#}
# {{ (parsed.subject|clean_subject or '(no subject)')|md_escape }}

- From: {{ parsed.author|safe_from|md_escape }}
- Date: {{ article.date.strftime('%Y-%m-%d %H:%M:%S %z') if article.date else '?' }}
- List: {{ inbox_name|md_escape }}
- Page: {{ site_base ~ article|msg_url(inbox_name) }}
{% if thread_view_url %}
- Thread: {{ site_base ~ thread_view_url|markdown_path }}
{% endif %}

{% if parsed.body %}
{% set text = parsed.body|render_body_text %}
{% set fence = text|md_fence %}
{{ fence }}text
{{ text }}
{{ fence }}
{% else %}
_(no body)_
{% endif %}
{% if parsed.attachments %}

## Attachments

{% for att in parsed.attachments %}
- [{{ (att.filename or '(unnamed)')|md_escape }}]({{ site_base ~ attachment_path(article, inbox_name, loop.index0) }}) ({{ att.content_type|md_escape }}, {{ att.content|length }} bytes)
{% endfor %}
{% endif %}
