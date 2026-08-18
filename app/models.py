"""
AutoApply - SQLAlchemy ORM Models
All database table definitions.
"""
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey
)
from sqlalchemy.orm import relationship
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class Profile(Base):
    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    full_name = Column(String(200), default="Siddhesh Santosh Sawant")
    first_name = Column(String(100), default="Siddhesh Santosh")
    last_name = Column(String(100), default="Sawant")
    email = Column(String(200), nullable=False, unique=True, default="siddhesh202001@gmail.com")
    phone = Column(String(50), default="+1 445-208-2299")
    location = Column(String(200), default="Washington, DC")
    country = Column(String(100), default="United States")
    linkedin_url = Column(String(500), default="https://www.linkedin.com/in/sss20/")
    github_url = Column(String(500), default="https://github.com/sids202001")
    portfolio_url = Column(String(500), default="https://siddhesh202001-portfolio.netlify.app")
    summary = Column(Text)
    work_authorization = Column(String(100), default="US Citizen / Authorized")
    sponsorship_required = Column(Boolean, default=True)
    authorized_us = Column(Boolean, default=True)
    desired_salary_min = Column(Integer, default=80000)
    desired_salary_max = Column(Integer, default=160000)
    years_experience = Column(Integer, default=2)
    resume_path = Column(String(500), default=None)

    # Detailed Education & Employment
    recent_degree = Column(String(100), default="Masters")
    school = Column(String(200), default="George Mason University")
    current_company = Column(String(200), default="Talviro INC")
    current_title = Column(String(200), default="Software Developer")

    # EEO / Demographics
    gender = Column(String(50), default="Male")
    hispanic_latino = Column(String(50), default="No")
    race_ethnicity = Column(String(100), default="Asian")
    disability_status = Column(String(50), default="No")
    veteran_status = Column(String(50), default="Non-Veteran")

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    experiences = relationship("Experience", back_populates="profile", cascade="all, delete-orphan")
    education = relationship("Education", back_populates="profile", cascade="all, delete-orphan")
    skills = relationship("Skill", back_populates="profile", cascade="all, delete-orphan")
    filters = relationship("JobFilter", back_populates="profile", cascade="all, delete-orphan")
    applications = relationship("Application", back_populates="profile", cascade="all, delete-orphan")


class Experience(Base):
    __tablename__ = "experiences"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False)
    company = Column(String(200), nullable=False)
    title = Column(String(200), nullable=False)
    start_date = Column(String(20))
    end_date = Column(String(20))
    is_current = Column(Boolean, default=False)
    description = Column(Text)
    location = Column(String(200))

    profile = relationship("Profile", back_populates="experiences")


class Education(Base):
    __tablename__ = "education"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False)
    school = Column(String(300), nullable=False)
    degree = Column(String(200))
    field_of_study = Column(String(200))
    start_date = Column(String(20))
    end_date = Column(String(20))
    gpa = Column(Float)

    profile = relationship("Profile", back_populates="education")


class Skill(Base):
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False)
    skill_name = Column(String(100), nullable=False)
    proficiency_level = Column(String(50))

    profile = relationship("Profile", back_populates="skills")


class JobFilter(Base):
    __tablename__ = "job_filters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False)
    name = Column(String(200), default="Default Filter")
    role_keywords = Column(Text)
    location = Column(String(200))
    remote_preference = Column(String(50), default="Any")
    min_salary = Column(Integer)
    max_salary = Column(Integer)
    experience_level = Column(String(50))
    excluded_companies = Column(Text)
    job_type = Column(String(50), default="Full-time")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)

    profile = relationship("Profile", back_populates="filters")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(300), nullable=False)
    company = Column(String(200), nullable=False)
    location = Column(String(200))
    salary_min = Column(Integer)
    salary_max = Column(Integer)
    job_type = Column(String(50))
    remote_type = Column(String(50))
    description = Column(Text)
    requirements = Column(Text)
    url = Column(String(1000), nullable=False)
    source = Column(String(100))
    match_score = Column(Float, default=0.0)
    posted_date = Column(String(50))
    scraped_at = Column(DateTime, default=utcnow)
    is_hidden = Column(Boolean, default=False)

    applications = relationship("Application", back_populates="job", cascade="all, delete-orphan")


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False)
    status = Column(String(50), default="saved")
    applied_at = Column(DateTime)
    notes = Column(Text)
    follow_up_date = Column(String(20))
    cover_letter = Column(Text)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    screenshot_path = Column(String(500))
    apply_mode = Column(String(50), default="review")
    fields_filled_count = Column(Integer, default=0)
    error_message = Column(Text)

    job = relationship("Job", back_populates="applications")
    profile = relationship("Profile", back_populates="applications")
