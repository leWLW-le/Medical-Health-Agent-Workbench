**English** | [简体中文](README.md)

# Medical Report Audit Agent

A platform for reviewing lab reports and asking questions grounded in report evidence. Users can sign in, upload a PDF or photo, read a patient-friendly summary, correct extracted results against the source page, and ask follow-up questions. Reports, corrections, and conversations are saved to the user’s account.

## Main application

- **Account workspace.** Sign in to access persistent text chats, uploaded report results, corrections, and follow-up questions.
- **Report reader.** Upload a PDF or photo through one entry point. Local OCR extracts lab rows, compares them with the report's reference ranges, and identifies fields to verify against the source page.
- **Patient-friendly checkup summary.** See flagged results with source-page links, answer relevant follow-up questions, and get guidance on preparing for a medical visit.
- **Explanations and evaluation.** Read a plain-language item definition, ask about printed values and ranges, and see the source page. A synthetic benchmark measures extraction, page location, missed abnormalities, and citations.
- **Audit demo.** A separate synthetic-record workspace demonstrates asynchronous batches, versioned review, and evidence Q&A.

Demo records are synthetic.

## Run locally

The default frontend uses the **account-based application**. Start the Spring Boot gateway and Python agents with Docker Compose or with the local H2 profile. Then open [Report Analysis](http://127.0.0.1:5173/#/report-audit) in the Health Assistant. [Account application setup](APPLICATION.md)

For standalone OCR and rule development, explicitly set `VITE_AUDIT_ASYNC=0` and `VITE_MANAGED_HEALTH_CHAT=0` so the frontend connects directly to FastAPI. [Standalone setup and evaluation](REPORT_AUDIT.md)

## Stack

React, TypeScript, Vite, and Tailwind CSS for the web client. Java 21, Spring Boot, Flyway, MySQL, and Redis for accounts, jobs, and audit versions. Python, FastAPI, LangGraph, PDFium, and RapidOCR for the agents, deterministic checks, and document processing.
