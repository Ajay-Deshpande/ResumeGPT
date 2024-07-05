import os
import time
import subprocess
from datetime import datetime
from typing import List, Optional
from bs4 import BeautifulSoup
import requests
from langchain.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableSequence
from langchain_core.output_parsers import StrOutputParser
from ..models.resume import (
    ResumeImproverOutput,
    ResumeSkillsMatcherOutput,
    ResumeSummarizerOutput,
    ResumeSectionHighlighterOutput,
)
from .. import utils
from .. import config
from .langchain_helpers import *
from .extractor import ExtractorLLM
from ..prompts import Prompts
from ..models.job_post import JobPost
from ..pdf_generation import ResumePDFGenerator


class ResumeImprover(ExtractorLLM):
    def __init__(self, url, resume_location=None, llm_kwargs: dict = None):
        """Initialize ResumeImprover with the job post URL and optional resume location."""
        super().__init__()
        self.job_post_html_data = None
        self.job_post_raw = None
        self.resume = None
        self.resume_yaml = None
        self.job_post = None
        self.parsed_job = None
        self.llm_kwargs = llm_kwargs or {}

        self.widget = None
        self.editing = False
        self.text_area = None
        self.button = None
        self.clean_url = None
        self.job_data_location = None
        self.yaml_loc = None

        self.url = url
        self.download_and_parse_job_post()
        self.resume_location = resume_location or config.DEFAULT_RESUME_PATH
        self.resume = utils.read_yaml(filename=self.resume_location)
        self.degrees = self._get_degrees(self.resume)
        self.basic_info = utils.get_dict_field(field="basic", data_dict=self.resume)
        self.education = utils.get_dict_field(field="education", data_dict=self.resume)
        self.experiences = utils.get_dict_field(
            field="experiences", data_dict=self.resume
        )
        self.skills = utils.get_dict_field(field="skills", data_dict=self.resume)
        self.summary = utils.get_dict_field(field="summary", data_dict=self.resume)

    def _extract_html_data(self):
        """Extract text content from HTML, removing all HTML tags."""
        try:
            soup = BeautifulSoup(self.job_post_html_data, "html.parser")
            self.job_post_raw = soup.get_text(separator=" ", strip=True)
        except Exception as e:
            config.logger.error(f"Failed to extract HTML data: {e}")
            raise

    def _download_url(self):
        """Download the content of the URL and return it as a string."""
        try:
            response = requests.get(self.url, headers=config.REQUESTS_HEADERS)
            response.raise_for_status()
            self.job_post_html_data = response.text
        except requests.RequestException as e:
            config.logger.error(f"Failed to download URL {self.url}: {e}")
            raise

    def download_and_parse_job_post(self):
        """Download and parse the job post from the provided URL."""
        self._download_url()
        self._extract_html_data()
        self.job_post = JobPost(self.job_post_raw)
        self.parsed_job = self.job_post.parse_job_post(verbose=False)

        # Extract the filename from the URL
        try:
            filename = self.parsed_job["company"] + "_" + self.parsed_job["job_title"]
            filename = filename.replace(" ", "_")
        except KeyError:
            if "://" in self.url:
                filename = self.url.split("://")[1]
            else:
                filename = self.url
            url_paths = filename.split("/")
            filename = url_paths[0]
            if len(url_paths) > 1:
                filename = filename + "." + url_paths[-1]
        self.clean_url = filename
        filepath = os.path.join(config.DATA_PATH, self.clean_url)
        self.job_data_location = filepath
        os.makedirs(self.job_data_location, exist_ok=True)
        utils.write_yaml(
            self.parsed_job, filename=os.path.join(self.job_data_location, "job.yaml")
        )

    def create_draft_tailored_resume(self):
        """Run a full review of the resume against the job post."""
        config.logger.info("Extracting matched skills...")

        self.skills = self.extract_matched_skills(verbose=False)
        config.logger.info("Writing summary...")
        self.summary = self.write_summary(verbose=False)
        config.logger.info("Updating bullet points...")
        self.experiences = self.rewrite_unedited_experiences(verbose=False)
        config.logger.info("Done updating...")
        self.yaml_loc = os.path.join(self.job_data_location, "resume.yaml")
        resume_dict = dict(
            editing=True,
            basic=self.basic_info,
            summary=self.summary,
            education=self.education,
            experiences=self.experiences,
            skills=self.skills,
        )
        utils.write_yaml(resume_dict, filename=self.yaml_loc)
        self.resume_yaml = utils.read_yaml(filename=self.yaml_loc)

        subprocess.run(["cursor", "-r", self.yaml_loc])

        while utils.read_yaml(filename=self.yaml_loc)["editing"]:
            time.sleep(5)
        config.logger.info("Saving PDF")
        self.create_pdf()

    def _launch_widget(self):
        """Launch a widget for editing the resume."""
        self.text_area = widgets.Textarea(
            value=self.resume_yaml,
            disabled=False,
            layout=widgets.Layout(width="80%", height="800px"),
        )
        display(self.text_area)
        self.button = widgets.Button(description="Submit")
        self.button.on_click(self._on_button_clicked)
        display(self.button)

    def _on_button_clicked(self, b):
        """Handle the button click event to submit the edited resume."""
        self.resume_yaml = self.text_area.value
        clear_output(wait=False)
        time.sleep(1)
        display(Markdown(self.resume_yaml))
        time.sleep(2)
        clear_output(wait=True)
        self.editing = False

    def _section_highlighter_chain(self, **chain_kwargs) -> RunnableSequence:
        """Create a chain for highlighting relevant resume sections."""
        prompt_msgs = Prompts.lookup["SECTION_HIGHLIGHTER"]
        prompt = ChatPromptTemplate(messages=prompt_msgs)

        llm = create_llm(**self.llm_kwargs)
        return prompt | llm | StrOutputParser()

    def _skills_matcher_chain(self, **chain_kwargs) -> RunnableSequence:
        """Create a chain for matching skills from the resume to the job posting."""
        prompt_msgs = Prompts.lookup["SKILLS_MATCHER"]
        prompt = ChatPromptTemplate(messages=prompt_msgs)

        llm = create_llm(**self.llm_kwargs)
        return prompt | llm | StrOutputParser()

    def _summary_writer_chain(self, **chain_kwargs) -> RunnableSequence:
        """Create a chain for writing a compelling resume summary."""
        prompt_msgs = Prompts.lookup["SUMMARY_WRITER"]
        prompt = ChatPromptTemplate(messages=prompt_msgs)

        llm = create_llm(**self.llm_kwargs)
        return prompt | llm | StrOutputParser()

    def _improver_chain(self, **chain_kwargs) -> RunnableSequence:
        """Create a chain for critiquing and improving the resume."""
        prompt_msgs = Prompts.lookup["IMPROVER"]
        prompt = ChatPromptTemplate(messages=prompt_msgs)

        llm = create_llm(**self.llm_kwargs)
        return prompt | llm | StrOutputParser()

    def _get_degrees(self, resume: dict):
        result = []
        for degrees in utils.generator_key_in_nested_dict("degrees", resume):
            for degree in degrees:
                if isinstance(degree["names"], list):
                    result.extend(degree["names"])
                elif isinstance(degree["names"], str):
                    result.append(degree["names"])
        return result

    def _format_skills_for_prompt(self, skills: list) -> list:
        result = []
        for cat in skills:
            curr = ""
            if cat.get("category", ""):
                curr += f"{cat['category']}: "
            if "skills" in cat:
                curr += "Proficient in "
                curr += ", ".join(cat["skills"])
                result.append(curr)
        return result

    def _get_cumulative_time_from_titles(self, titles) -> int:
        result = 0.0
        for t in titles:
            if "startdate" in t and "enddate" in t:
                if t["enddate"] == "current":
                    last_date = datetime.today().strftime("%Y-%m-%d")
                else:
                    last_date = t["enddate"]
            result += datediff_years(start_date=t["startdate"], end_date=last_date)
        return round(result)

    def _format_experiences_for_prompt(self) -> list:
        result = []
        for exp in self.experiences:
            curr = ""
            if "titles" in exp:
                exp_time = self._get_cumulative_time_from_titles(exp["titles"])
                curr += f"{exp_time} years experience in:"
            if "highlights" in exp:
                curr += format_list_as_string(exp["highlights"], list_sep="\n  - ")
                curr += "\n"
                result.append(curr)
        return result

    def _combine_skills_in_category(self, l1: list[str], l2: list[str]):
        """Combines l2 into l1 without lowercase duplicates"""
        l1_lower = {i.lower() for i in l1}
        for i in l2:
            if i.lower() not in l1_lower:
                l1.append(i)

    def _combine_skill_lists(self, l1: list[dict], l2: list[dict]):
        """Combine l2 skills list into l1 without duplicating lowercase category or skills"""
        l1_categories_lowercase = {s["category"].lower(): i for i, s in enumerate(l1)}
        for s in l2:
            if s["category"].lower() in l1_categories_lowercase:
                self._combine_skills_in_category(
                    l1[l1_categories_lowercase[s["category"].lower()]]["skills"],
                    s["skills"],
                )
            else:
                l1.append(s)

    def _print_debug_message(self, chain_kwargs: dict, chain_output_unformatted: str):
        message = "Final answer is missing from the chain output."

    def rewrite_section(self, section: list | str, **chain_kwargs) -> dict:
        chain = self._section_highlighter_chain(**chain_kwargs)

        chain_inputs = format_prompt_inputs_as_strings(
            prompt_inputs=chain.input_schema().dict(),
            **self.parsed_job,
            degrees=self.degrees,
            experiences=utils.dict_to_yaml_string(dict(Experiences=self.experiences)),
            education=utils.dict_to_yaml_string(dict(Education=self.education)),
            skills=self._format_skills_for_prompt(self.skills),
            summary=self.summary,
        )
        chain_inputs["section"] = section

        section_revised_unformatted = chain.invoke(chain_inputs)

        section_revised = self.extract_from_input(
            pydantic_object=ResumeSectionHighlighterOutput,
            input=section_revised_unformatted,
        )

        section_revised = sorted(
            section_revised["final_answer"], key=lambda d: d["relevance"] * -1
        )

        return [s["highlight"] for s in section_revised]

    def rewrite_unedited_experiences(self, **chain_kwargs) -> dict:
        result = []
        for exp in self.experiences:
            exp = dict(exp)
            exp["highlights"] = self.rewrite_section(section=exp, **chain_kwargs)
            result.append(exp)

        return result

    def extract_matched_skills(self, **chain_kwargs) -> dict:
        chain = self._skills_matcher_chain(**chain_kwargs)

        chain_inputs = format_prompt_inputs_as_strings(
            prompt_inputs=chain.input_schema().dict(),
            **self.parsed_job,
            degrees=self.degrees,
            experiences=utils.dict_to_yaml_string(dict(Experiences=self.experiences)),
            education=utils.dict_to_yaml_string(dict(Education=self.education)),
            skills=self._format_skills_for_prompt(self.skills),
            summary=self.summary,
        )
        extracted_skills_unformatted = chain.invoke(chain_inputs)

        extracted_skills = self.extract_from_input(
            pydantic_object=ResumeSkillsMatcherOutput,
            input=extracted_skills_unformatted,
        )

        if not extracted_skills or "final_answer" not in extracted_skills:
            return None

        extracted_skills = extracted_skills["final_answer"]
        result = []

        if "technical_skills" in extracted_skills:
            result.append(
                dict(category="Technical", skills=extracted_skills["technical_skills"])
            )

        if "non_technical_skills" in extracted_skills:
            result.append(
                dict(
                    category="Non-technical",
                    skills=extracted_skills["non_technical_skills"],
                )
            )

        self._combine_skill_lists(result, self.skills)

        return result

    def write_summary(self, **chain_kwargs) -> dict:
        chain = self._summary_writer_chain(**chain_kwargs)
        chain_inputs = format_prompt_inputs_as_strings(
            prompt_inputs=chain.input_schema().dict(),
            **self.parsed_job,
            degrees=self.degrees,
            experiences=utils.dict_to_yaml_string(dict(Experiences=self.experiences)),
            education=utils.dict_to_yaml_string(dict(Education=self.education)),
            skills=self._format_skills_for_prompt(self.skills),
            summary=self.summary,
        )

        summary_unformatted = chain.invoke(chain_inputs)
        summary = self.extract_from_input(
            pydantic_object=ResumeSummarizerOutput, input=summary_unformatted
        )
        if not summary or "final_answer" not in summary:
            return None
        return summary["final_answer"]

    def suggest_improvements(self, **chain_kwargs) -> dict:
        chain = self._improver_chain(**chain_kwargs)
        chain_inputs = format_prompt_inputs_as_strings(
            prompt_inputs=chain.input_schema().dict(),
            **self.parsed_job,
            degrees=self.degrees,
            experiences=utils.dict_to_yaml_string(dict(Experiences=self.experiences)),
            education=utils.dict_to_yaml_string(dict(Education=self.education)),
            skills=self._format_skills_for_prompt(self.skills),
            summary=self.summary,
        )

        improvements_unformatted = chain.invoke(chain_inputs)

        improvements = self.extract_from_input(
            pydantic_object=ResumeImproverOutput, input=improvements_unformatted
        )
        if not improvements or "final_answer" not in improvements:
            return None
        return improvements["final_answer"]

    def finalize(self) -> dict:
        return dict(
            basic=self.basic_info,
            summary=self.summary,
            education=self.education,
            experiences=self.experiences,
            skills=self.skills,
        )

    def create_pdf(self):
        parsed_yaml = utils.read_yaml(filename=self.yaml_loc)
        result = {
            "education": [
                {"degree": degree["names"][0], "university": edu["school"]}
                for edu in sorted(
                    parsed_yaml["education"],
                    key=lambda x: "Wisconsin" not in x["school"],
                )
                for degree in edu["degrees"]
            ],
            "objective": parsed_yaml["summary"],
            "experience": [
                {
                    "title": title["name"],
                    "company": (
                        "Capital One"
                        if "Capital One" in exp["company"]
                        else exp["company"]
                    ),
                    "location": "New York, NY",
                    "duration": (
                        f"{'2012' if 'Capital One' in exp['company'] else title['startdate']}-{title['enddate']}"
                        if "enddate" in title
                        else "2024"
                    ),
                    "description": exp["highlights"],
                }
                for exp in parsed_yaml["experiences"]
                for title in exp["titles"]
            ],
            "skills": [
                f"Technical: {', '.join(parsed_yaml['skills'][0]['skills'])}",
                f"Soft Skills: {', '.join(parsed_yaml['skills'][1]['skills'])}",
            ],
        }
        pdf_location = os.path.join(self.job_data_location, "TylerKline.pdf")
        pdf_generator = ResumePDFGenerator()
        pdf_generator.generate_resume(file_path=pdf_location, data=result)
        open_file_commands = config.OPEN_FILE_COMMAND.split(" ") + [pdf_location]
        subprocess.run(open_file_commands)
        return pdf_location
