"""
AutoApply - Resume Parser Service
Extracts text and structured profile data from uploaded or local resume PDFs.
"""
import logging
import os
import re

logger = logging.getLogger(__name__)


class ResumeParser:
    """
    Extracts text and key profile information from PDF files.
    """

    COMMON_SKILLS = [
        "Python", "Java", "JavaScript", "TypeScript", "C++", "C#", "Go", "Rust",
        "SQL", "PostgreSQL", "MySQL", "MongoDB", "Redis", "Elasticsearch",
        "AWS", "GCP", "Azure", "Docker", "Kubernetes", "Terraform",
        "FastAPI", "Django", "Flask", "React", "Node.js", "Next.js",
        "Spark", "Hadoop", "Airflow", "ETL", "REST API", "Microservices",
        "Git", "CI/CD", "Linux", "PyTorch", "TensorFlow", "Pandas", "NumPy",
        "Snowflake", "Redshift", "BigQuery", "dbt", "Kafka", "RabbitMQ", "GraphQL",
        "Spring Boot", "Ruby", "Scala", "Kotlin", "Swift", "Vue.js", "Angular",
        "Lambda", "Step Functions", "S3", "DynamoDB", "CloudFormation", "CDK",
        "Jenkins", "GitHub Actions", "ArgoCD", "Prometheus", "Grafana",
        "Agile", "Scrum", "JIRA", "Machine Learning", "Deep Learning", "NLP",
        "Computer Vision", "LLM", "RAG", "LangChain", "OpenAI"
    ]

    def get_full_text(self, file_path: str) -> str:
        """Return the raw resume text for TF-IDF matching."""
        result = self.parse_pdf(file_path)
        return result["raw_text"] if result["success"] else ""

    def parse_pdf(self, file_path: str) -> dict:
        """
        Extract text from a PDF resume using pypdf, pdfplumber, or PyPDF2.
        """
        result = {
            "filename": os.path.basename(file_path),
            "raw_text": "",
            "success": False,
            "error": None,
        }

        if not os.path.exists(file_path):
            result["error"] = f"File not found: {file_path}"
            logger.error(result["error"])
            return result

        # 1. Try pypdf
        try:
            import pypdf
            reader = pypdf.PdfReader(file_path)
            text_parts = [page.extract_text() for page in reader.pages if page.extract_text()]
            if text_parts:
                result["raw_text"] = "\n".join(text_parts)
                result["success"] = True
                logger.info(f"Parsed PDF with pypdf: {file_path}")
                return result
        except Exception as e:
            logger.info(f"pypdf failed or not available: {e}")

        # 2. Try pdfplumber
        try:
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                text_parts = [page.extract_text() for page in pdf.pages if page.extract_text()]
            if text_parts:
                result["raw_text"] = "\n".join(text_parts)
                result["success"] = True
                logger.info(f"Parsed PDF with pdfplumber: {file_path}")
                return result
        except Exception as e:
            logger.info(f"pdfplumber failed or not available: {e}")

        # 3. Try PyPDF2
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(file_path)
            text_parts = [page.extract_text() for page in reader.pages if page.extract_text()]
            if text_parts:
                result["raw_text"] = "\n".join(text_parts)
                result["success"] = True
                logger.info(f"Parsed PDF with PyPDF2: {file_path}")
                return result
        except Exception as e:
            logger.info(f"PyPDF2 failed or not available: {e}")

        result["error"] = "Failed to extract text from PDF."
        return result

    def extract_structured_data(self, raw_text: str) -> dict:
        """
        Analyze extracted resume text to identify skills, years of experience, and role keywords.
        """
        data = {
            "skills": [],
            "years_experience": None,
            "role_keywords": [],
            "summary_snippet": ""
        }

        if not raw_text:
            return data

        # Skill extraction
        text_lower = raw_text.lower()
        found_skills = []
        for skill in self.COMMON_SKILLS:
            pattern = r'\b' + re.escape(skill.lower()) + r'\b'
            if re.search(pattern, text_lower):
                found_skills.append(skill)
        data["skills"] = found_skills

        # Experience extraction (e.g. "2+ years", "3 years of experience")
        exp_match = re.search(r'(\d+)\+?\s*years?\s*(?:of)?\s*experience', text_lower)
        if exp_match:
            try:
                data["years_experience"] = int(exp_match.group(1))
            except ValueError:
                pass

        # Role keywords extraction
        role_candidates = [
            "Software Engineer", "Data Engineer", "Backend Engineer", "Full Stack Developer",
            "Cloud Engineer", "DevOps Engineer", "ML Engineer", "Software Developer"
        ]
        found_roles = [role for role in role_candidates if re.search(r'\b' + re.escape(role.lower()) + r'\b', text_lower)]
        data["role_keywords"] = found_roles or ["Software Engineer", "Data Engineer"]

        # Summary snippet
        lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
        if len(lines) > 2:
            data["summary_snippet"] = " ".join(lines[1:5])[:300]

        return data

    def get_full_text(self, file_path: str) -> str:
        """Return the raw resume text for TF-IDF matching."""
        result = self.parse_pdf(file_path)
        return result["raw_text"] if result["success"] else ""

