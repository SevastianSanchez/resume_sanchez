#!/usr/bin/env python3
import argparse
import datetime
import html
import re
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pdfplumber

MONTH_PATTERN = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December|Expected|Present|Graduated)",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(r"https?://\S+")
PHONE_PATTERN = re.compile(r"\+?\d[\d\-()\s]{7,}\d")
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LOCATION_PATTERN = re.compile(r"[A-Za-z .'-]+,\s?[A-Z]{2}(?:\s\d{5})?$")
LINKEDIN_PATTERN = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/[^\s|]+", re.IGNORECASE)
GITHUB_PATTERN = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[^\s|]+", re.IGNORECASE)
# Typical resume header occupies the first few lines before the first section heading.
HEADER_FALLBACK_LINES = 5


def normalize_text(text: str) -> str:
    text = text.replace("\u2502", "|")
    text = re.sub(r"-\n(?=[a-z])", "-", text)
    return text


def normalize_lines(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    return lines


def section(lines: list[str], start: str, end_markers: list[str]) -> list[str]:
    try:
        start_idx = lines.index(start)
    except ValueError:
        return []

    end_idx = len(lines)
    for marker in end_markers:
        try:
            idx = lines.index(marker, start_idx + 1)
            end_idx = min(end_idx, idx)
        except ValueError:
            pass
    return lines[start_idx + 1 : end_idx]


def cleanup_trailing_location(value: str) -> str:
    return value.strip()


def should_join_wrapped_line(base: str, next_line: str) -> bool:
    base_trim = base.rstrip()
    if not next_line or next_line.startswith("•"):
        return False
    if base_trim.endswith((" and", " in", " the", " &", " of")):
        return True
    return False


def looks_like_location(value: str) -> bool:
    return bool(LOCATION_PATTERN.fullmatch(value))


def split_title_date(value: str) -> tuple[str, str]:
    match = MONTH_PATTERN.search(value)
    if not match:
        return value.strip(), ""
    title = value[: match.start()].strip(" ,-|")
    date = value[match.start() :].strip()
    if re.search(r"\b\d{4}\b", date):
        return title, date
    return value.strip(), ""


def parse_header(lines: list[str]) -> dict:
    education_idx = lines.index("EDUCATION") if "EDUCATION" in lines else min(HEADER_FALLBACK_LINES, len(lines))
    top = lines[:education_idx]

    name = top[0] if top else "Resume"
    location = next((line for line in top if looks_like_location(line)), "")

    email = ""
    phone = ""
    linkedin = ""
    github = ""

    for line in top:
        if not email:
            m = EMAIL_PATTERN.search(line)
            if m:
                email = m.group(0)
        if not phone:
            m = PHONE_PATTERN.search(line)
            if m:
                phone = m.group(0)
        if not linkedin:
            m = LINKEDIN_PATTERN.search(line)
            if m:
                linkedin = m.group(0).rstrip(".,);")
        if not github:
            m = GITHUB_PATTERN.search(line)
            if m:
                github = m.group(0).rstrip(".,);")

    return {
        "name": name,
        "location": location,
        "phone": phone,
        "email": email,
        "linkedin": linkedin,
        "github": github,
    }


def is_experience_entry_start(lines: list[str], idx: int) -> bool:
    return (
        idx + 1 < len(lines)
        and not lines[idx].startswith("•")
        and not lines[idx + 1].startswith("•")
        and bool(split_title_date(lines[idx + 1])[1])
    )


def parse_experience(lines: list[str]) -> list[dict]:
    jobs = []
    i = 0
    while i < len(lines):
        if not is_experience_entry_start(lines, i):
            i += 1
            continue

        company = cleanup_trailing_location(lines[i])
        title, date = split_title_date(lines[i + 1])
        i += 2

        bullets = []
        while i < len(lines):
            if lines[i].startswith("•"):
                bullet = lines[i].lstrip("• ").strip()
                i += 1
                while i < len(lines) and not lines[i].startswith("•") and not is_experience_entry_start(lines, i):
                    bullet = f"{bullet} {lines[i]}".strip()
                    i += 1
                bullets.append(bullet)
                continue
            if is_experience_entry_start(lines, i):
                break
            i += 1

        jobs.append({"title": title, "company": company, "date": date, "bullets": bullets})

    return jobs


def is_education_header(value: str) -> bool:
    return any(token in value for token in ("UNIVERSITY", "COLLEGE", "INSTITUTE", "SCHOOL"))


def parse_education(lines: list[str]) -> list[dict]:
    entries = []
    i = 0
    while i < len(lines):
        if not is_education_header(lines[i]):
            i += 1
            continue

        institution_line = lines[i]
        while (
            i + 1 < len(lines)
            and not is_education_header(lines[i + 1])
            and not split_title_date(lines[i + 1])[1]
            and should_join_wrapped_line(institution_line, lines[i + 1])
        ):
            i += 1
            institution_line = f"{institution_line} {lines[i]}".strip()
        institution = cleanup_trailing_location(institution_line)
        i += 1
        if i >= len(lines):
            break

        degree, date = split_title_date(lines[i])
        i += 1

        details = []
        while i < len(lines) and not is_education_header(lines[i]):
            if details and (
                details[-1].endswith(",")
                or details[-1].endswith("-")
                or details[-1].endswith("(")
                or lines[i][0].islower()
            ):
                details[-1] = f"{details[-1]} {lines[i]}".strip()
            else:
                details.append(lines[i])
            i += 1

        entries.append(
            {
                "degree": degree,
                "institution": institution,
                "date": date,
                "details": details,
            }
        )

    return entries


def is_project_header(value: str) -> bool:
    return bool(re.search(r"(Thesis|Study|Project)", value)) and not value.startswith("•")


def parse_projects(lines: list[str]) -> list[dict]:
    projects = []
    i = 0

    while i < len(lines):
        if looks_like_location(lines[i]) or not is_project_header(lines[i]):
            i += 1
            continue

        title_line = lines[i]
        while (
            i + 1 < len(lines)
            and not lines[i + 1].startswith("•")
            and not is_project_header(lines[i + 1])
            and not split_title_date(lines[i + 1])[1]
            and should_join_wrapped_line(title_line, lines[i + 1])
        ):
            i += 1
            title_line = f"{title_line} {lines[i]}".strip()
        title = cleanup_trailing_location(title_line)
        i += 1

        descriptor_lines = []
        while i < len(lines) and not lines[i].startswith("•") and not is_project_header(lines[i]):
            if not looks_like_location(lines[i]):
                descriptor_lines.append(lines[i])
            i += 1

        subtitle_parts = []
        date = ""
        for descriptor in descriptor_lines:
            line_title, line_date = split_title_date(descriptor)
            if line_date and not date:
                date = line_date
                if line_title:
                    subtitle_parts.append(line_title.strip(" -"))
            else:
                subtitle_parts.append(descriptor)

        bullets = []
        while i < len(lines):
            if lines[i].startswith("•"):
                bullet = lines[i].lstrip("• ").strip()
                i += 1
                while (
                    i < len(lines)
                    and not lines[i].startswith("•")
                    and not is_project_header(lines[i])
                    and not looks_like_location(lines[i])
                ):
                    bullet = f"{bullet} {lines[i]}".strip()
                    i += 1
                bullets.append(bullet)
                continue
            if i < len(lines) and (is_project_header(lines[i]) or looks_like_location(lines[i])):
                break
            i += 1

        projects.append(
            {
                "title": title,
                "subtitle": " ".join(subtitle_parts).strip(),
                "date": date,
                "bullets": bullets,
            }
        )

    return projects


def parse_publications(lines: list[str], name: str) -> list[dict]:
    surname = name.split()[-1] if name else ""
    filtered = []
    for line in lines:
        lower = line.lower()
        if lower == name.lower():
            continue
        if looks_like_location(line):
            continue
        if LINKEDIN_PATTERN.search(line) or GITHUB_PATTERN.search(line):
            continue
        if EMAIL_PATTERN.search(line) or PHONE_PATTERN.search(line):
            continue
        filtered.append(line)

    entries = []
    current = ""
    for line in filtered:
        is_new_entry = bool(surname) and line.lower().startswith(surname.lower())
        if is_new_entry and current:
            entries.append(current.strip())
            current = line
        else:
            current = f"{current} {line}".strip()
    if current:
        entries.append(current.strip())

    publications = []
    for entry in entries:
        raw_urls = URL_PATTERN.findall(entry)
        urls = [url.rstrip(".,);") for url in raw_urls]
        text = entry
        for url in raw_urls:
            text = text.replace(url, "")
        text = re.sub(r"\s+", " ", text).strip().rstrip(",")
        publications.append({"text": text, "urls": urls})

    return publications


def parse_skills(lines: list[str]) -> dict:
    groups = {
        "Research Certifications": [],
        "Software": [],
        "Language": [],
    }

    current = ""
    for line in lines:
        if line.startswith("Research Certifications:"):
            current = "Research Certifications"
            groups[current].append(line.split(":", 1)[1].strip())
        elif line.startswith("Software:"):
            current = "Software"
            groups[current].append(line.split(":", 1)[1].strip())
        elif line.startswith("Language:"):
            current = "Language"
            groups[current].append(line.split(":", 1)[1].strip())
        elif current:
            groups[current].append(line)

    return {
        "Software & Technical Skills": " ".join(groups["Software"]).strip(),
        "Languages": " ".join(groups["Language"]).strip(),
        "Research Certifications": " ".join(groups["Research Certifications"]).strip(),
    }


def extract_existing_summary(index_path: Path) -> str:
    if not index_path.exists():
        return "Professional resume generated from PDF content."

    content = index_path.read_text(encoding="utf-8")
    match = re.search(
        r'<section class="summary">.*?<p>\s*(.*?)\s*</p>.*?</section>',
        content,
        flags=re.DOTALL,
    )
    if not match:
        return "Professional resume generated from PDF content."

    summary = re.sub(r"\s+", " ", match.group(1)).strip()
    return html.unescape(summary)


def build_resume_data(pdf_path: Path) -> dict:
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)

    text = normalize_text(text)
    lines = normalize_lines(text)
    header = parse_header(lines)

    return {
        "header": header,
        "education": parse_education(section(lines, "EDUCATION", ["RELEVANT EXPERIENCE"])),
        "experience": parse_experience(section(lines, "RELEVANT EXPERIENCE", ["PUBLICATIONS"])),
        "publications": parse_publications(section(lines, "PUBLICATIONS", ["PROJECTS"]), header["name"]),
        "projects": parse_projects(section(lines, "PROJECTS", ["ADDITIONAL INFORMATION"])),
        "skills": parse_skills(section(lines, "ADDITIONAL INFORMATION", [])),
    }


def validate_and_format_profile_url(value: str, allowed_domain: str) -> tuple[str, str]:
    if not value:
        return "", ""
    candidate = value.strip().rstrip(".,);")
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    host = parsed.netloc.lower()
    if host not in {allowed_domain, f"www.{allowed_domain}"}:
        return "", ""
    if not parsed.path or parsed.path == "/":
        return "", ""
    href = urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    display = f"{allowed_domain}{parsed.path}".rstrip("/")
    return href, display


def render_header(header: dict) -> str:
    linkedin_href, linkedin_text = validate_and_format_profile_url(header.get("linkedin", ""), "linkedin.com")
    github_href, github_text = validate_and_format_profile_url(header.get("github", ""), "github.com")
    contact_line = " | ".join(part for part in [header.get("location", ""), header.get("phone", "")] if part)

    return "\n".join(
        [
            "        <header>",
            f"            <h1>{html.escape(header.get('name', 'Resume'))}</h1>",
            '            <div class="contact-info">',
            f"                <p>{html.escape(contact_line)}</p>",
            f"                <p>Email: <a href=\"mailto:{html.escape(header.get('email', ''))}\">{html.escape(header.get('email', ''))}</a></p>",
            f"                <p>LinkedIn: <a href=\"{html.escape(linkedin_href)}\" target=\"_blank\" rel=\"noopener noreferrer\">{html.escape(linkedin_text)}</a></p>",
            f"                <p>GitHub: <a href=\"{html.escape(github_href)}\" target=\"_blank\" rel=\"noopener noreferrer\">{html.escape(github_text)}</a></p>",
            "            </div>",
            '            <div class="pdf-download">',
            '                <a href="assets/resume.pdf" class="download-button" download aria-label="Download resume in PDF format">Download PDF Resume</a>',
            "            </div>",
            "        </header>",
        ]
    )


def render_experience(items: list[dict]) -> str:
    blocks = []
    for item in items:
        bullets = "\n".join(f"                    <li>{html.escape(bullet)}</li>" for bullet in item["bullets"])
        blocks.append(
            "\n".join(
                [
                    '            <div class="job">',
                    f"                <h3>{html.escape(item['title'])}</h3>",
                    f"                <p class=\"company\">{html.escape(item['company'])}</p>",
                    f"                <p class=\"date\">{html.escape(item['date'])}</p>",
                    "                <ul>",
                    bullets,
                    "                </ul>",
                    "            </div>",
                ]
            )
        )
    return "\n\n".join(blocks)


def render_education(items: list[dict]) -> str:
    blocks = []
    for item in items:
        details = "\n".join(f"                <p>{html.escape(detail)}</p>" for detail in item["details"])
        lines = [
            '            <div class="degree">',
            f"                <h3>{html.escape(item['degree'])}</h3>",
            f"                <p class=\"institution\">{html.escape(item['institution'])}</p>",
        ]
        if item["date"]:
            lines.append(f"                <p class=\"date\">{html.escape(item['date'])}</p>")
        if details:
            lines.append(details)
        lines.append("            </div>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_skills(skills: dict) -> str:
    return "\n\n".join(
        "\n".join(
            [
                '            <div class="skill-category">',
                f"                <h3>{html.escape(label)}</h3>",
                f"                <p>{html.escape(value)}</p>",
                "            </div>",
            ]
        )
        for label, value in skills.items()
        if value
    )


def render_projects(items: list[dict]) -> str:
    blocks = []
    for item in items:
        bullets = " ".join(html.escape(bullet) for bullet in item["bullets"])
        description_top = html.escape(item["subtitle"]) if item["subtitle"] else html.escape(item["title"])
        date = f" ({html.escape(item['date'])})" if item["date"] else ""
        description = f"<strong>{description_top}</strong>{date}"
        if bullets:
            description += f"<br>{bullets}"
        blocks.append(
            "\n".join(
                [
                    '            <div class="project">',
                    f"                <h3>{html.escape(item['title'])}</h3>",
                    "                <p class=\"project-description\">",
                    f"                    {description}",
                    "                </p>",
                    "            </div>",
                ]
            )
        )
    return "\n\n".join(blocks)


def render_publications(items: list[dict]) -> str:
    blocks = []
    for item in items:
        url_links = "\n".join(
            f"                    <a href=\"{html.escape(url)}\" target=\"_blank\" rel=\"noopener noreferrer\">{html.escape(url)}</a>"
            for url in item["urls"]
        )
        lines = [
            '            <div class="publication">',
            "                <p>",
            f"                    {html.escape(item['text'])}",
        ]
        if url_links:
            lines.append(url_links)
        lines += ["                </p>", "            </div>"]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_html(data: dict, summary: str) -> str:
    year = datetime.datetime.now(datetime.timezone.utc).year
    name = html.escape(data["header"].get("name", "Resume"))

    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
    <meta name=\"description\" content=\"Professional resume of {name} - View my work experience, education, skills, and projects.\">
    <title>{name} - Resume</title>
    <link rel=\"stylesheet\" href=\"style.css\">
</head>
<body>
    <div class=\"container\">
{render_header(data['header'])}

        <section class=\"summary\">
            <h2>Professional Summary</h2>
            <p>
                {html.escape(summary)}
            </p>
        </section>

        <section class=\"experience\">
            <h2>Relevant Experience</h2>
{render_experience(data['experience'])}
        </section>

        <section class=\"education\">
            <h2>Education</h2>
{render_education(data['education'])}
        </section>

        <section class=\"skills\">
            <h2>Skills</h2>
{render_skills(data['skills'])}
        </section>

        <section class=\"projects\">
            <h2>Research Projects</h2>
{render_projects(data['projects'])}
        </section>

        <section class=\"publications\">
            <h2>Publications</h2>
{render_publications(data['publications'])}
        </section>

        <footer>
            <p>&copy; {year} {name}. All rights reserved.</p>
        </footer>
    </div>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate index.html resume from PDF")
    parser.add_argument("--pdf", default="assets/resume.pdf", help="Path to resume PDF")
    parser.add_argument("--output", default="index.html", help="Output HTML path")
    parser.add_argument("--template", default="index.html", help="Template HTML used to preserve summary")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise FileNotFoundError(f"Resume PDF not found: {pdf_path}")

    data = build_resume_data(pdf_path)
    summary = extract_existing_summary(Path(args.template))
    Path(args.output).write_text(render_html(data, summary), encoding="utf-8")


if __name__ == "__main__":
    main()
