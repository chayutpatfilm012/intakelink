"""Fact field names shared by extraction (C4), matching (C6) and the guard (C7).

Kept in its own module so the extraction service can name fields without
importing anything that reads the Matter Index.
"""
MATTER_NUMBER = "matterNumber"
CLAIM_NUMBER = "claimNumber"
CLIENT_NAME = "clientName"
DATE_OF_BIRTH = "dateOfBirth"
INCIDENT_DATE = "incidentDate"
INSURER = "insurer"
PROVIDER = "provider"
DOCUMENT_DATE = "documentDate"   # not used for matching; used by duplicate checks

ALL_FIELDS = (MATTER_NUMBER, CLAIM_NUMBER, CLIENT_NAME, DATE_OF_BIRTH,
              INCIDENT_DATE, INSURER, PROVIDER, DOCUMENT_DATE)

DOCUMENT_TYPES = ("medical_bill", "medical_record", "police_report", "insurer_letter",
                  "wage_statement", "correspondence", "other")
