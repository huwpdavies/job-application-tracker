"""Realistic sample emails (invented companies) with the classification Claude should produce for each.

`html` is what Graph would return as the body. `classification` is what the mocked Claude returns in tests; the
optional live test (tests/test_live_classification.py) checks the real model agrees with `expect`.
"""


def _cls(**kw):
    base = dict(is_job_related=True, company=None, role_title=None, location=None, job_reference=None, source=None,
                interview_datetime=None, interview_format=None, interview_stage=None, summary="", confidence=0.95)
    base.update(kw)
    return base


LINKEDIN_CONFIRMATION = dict(
    sender="jobs-noreply@linkedin.com",
    subject="Huw, your application was sent to Northwind Analytics",
    received="2026-09-22T08:14:00Z",
    html="""<html><body><table><tr><td><h2>Your application was sent to Northwind Analytics</h2>
<p>Senior Data Analyst</p><p>Northwind Analytics &middot; Manchester, England, United Kingdom (Hybrid)</p>
<p>Applied on September 22, 2026</p>
<p><a href="https://www.linkedin.com/comm/jobs/view/3912345678?trk=eml">View job</a></p>
<p>Next steps: the hiring team will review your application. We'll let you know if they get in touch.</p>
<hr><p>You are receiving LinkedIn notification emails.</p><p>Unsubscribe: <a href="https://www.linkedin.com/e/v2?e=abc">here</a></p>
<p>This email was intended for Huw Davies. LinkedIn Corporation, 1000 W Maude Ave, Sunnyvale, CA 94085</p>
</td></tr></table></body></html>""",
    expect=dict(category="application_confirmation", company="Northwind Analytics", role_contains="Data Analyst"),
    classification=_cls(category="application_confirmation", company="Northwind Analytics", role_title="Senior Data Analyst",
                        location="Manchester", source="LinkedIn", summary="Application sent to Northwind Analytics."),
)

WORKDAY_CONFIRMATION = dict(
    sender="northbridge@myworkday.com",
    subject="Thank you for applying to Northbridge Insurance Group",
    received="2026-09-18T15:40:00Z",
    html="""<html><body><p>Dear Huw,</p>
<p>Thank you for applying for the position of <b>Pricing Analyst (R-104522)</b> at Northbridge Insurance Group.
We have received your application and our recruitment team will review it.</p>
<p>Location: Leeds, United Kingdom</p>
<p>If your profile matches the requirements, we will contact you to discuss next steps. You can check your application status at any time by signing in to your candidate account.</p>
<p>Kind regards,<br>Talent Acquisition<br>Northbridge Insurance Group</p>
<p>This is an automated message, please do not reply.</p></body></html>""",
    expect=dict(category="application_confirmation", company="Northbridge Insurance Group", job_reference="R-104522"),
    classification=_cls(category="application_confirmation", company="Northbridge Insurance Group", role_title="Pricing Analyst",
                        location="Leeds", job_reference="R-104522", source="Workday", summary="Application received."),
)

HARLOW_CONFIRMATION = dict(
    sender="jobs@reed.co.uk",
    subject="Application sent: Business Analyst at Harlow & Pine",
    received="2026-09-10T09:00:00Z",
    html="<html><body><p>Your application for <b>Business Analyst</b> at Harlow &amp; Pine has been sent.</p></body></html>",
    expect=dict(category="application_confirmation", company="Harlow & Pine"),
    classification=_cls(category="application_confirmation", company="Harlow & Pine Ltd", role_title="Business Analyst",
                        source="Reed", summary="Application sent."),
)

REJECTION = dict(
    sender="recruitment@harlowandpine.co.uk",
    subject="Your application for Business Analyst - Harlow & Pine",
    received="2026-09-30T10:05:00Z",
    html="""<html><body><p>Dear Huw,</p><p>Thank you for your interest in the Business Analyst role at Harlow &amp; Pine and for taking the time to apply.</p>
<p>After careful consideration, we have decided not to progress your application on this occasion. We received a large number of applications and, unfortunately, we are unable to offer you an interview.</p>
<p>We wish you every success in your job search.</p><p>Kind regards,<br>The Harlow &amp; Pine Recruitment Team</p></body></html>""",
    expect=dict(category="rejection", company="Harlow & Pine"),
    classification=_cls(category="rejection", company="Harlow & Pine", role_title="Business Analyst", summary="Not progressing."),
)

INTERVIEW_INVITE_WITH_TIME = dict(
    sender="jessica.moore@brightwaterenergy.com",
    subject="Interview confirmation - Reporting Analyst, Brightwater Energy",
    received="2026-10-01T09:30:00Z",
    html="""<html><body><p>Hi Huw,</p>
<p>Thanks for applying for the Reporting Analyst position (ref BE-2291) at Brightwater Energy. We'd like to invite you to a first-round video interview.</p>
<p><b>Date:</b> Tuesday 13 October 2026<br><b>Time:</b> 2:00pm - 2:45pm (UK time)<br><b>Format:</b> Microsoft Teams</p>
<p>You'll meet Jessica Moore (Head of Insight) and Dan Okafor. Please reply to confirm you can make it.</p>
<p>Best wishes,<br>Jessica Moore<br>Head of Insight, Brightwater Energy</p></body></html>""",
    expect=dict(category="interview_scheduled", company="Brightwater Energy", interview_utc="2026-10-13T13:00:00+00:00", format="video"),
    classification=_cls(category="interview_scheduled", company="Brightwater Energy", role_title="Reporting Analyst", job_reference="BE-2291",
                        interview_datetime="2026-10-13T14:00:00+01:00", interview_format="video", interview_stage="first round",
                        summary="First-round video interview on 13 Oct at 2pm."),
)

RESCHEDULE = dict(
    sender="jessica.moore@brightwaterenergy.com",
    subject="Re: Interview confirmation - Reporting Analyst, Brightwater Energy - NEW TIME",
    received="2026-10-08T16:20:00Z",
    html="""<html><body><p>Hi Huw,</p><p>Apologies - something has come up and we need to move your interview for the Reporting Analyst role.
Could we do <b>Thursday 15 October at 10:30am</b> instead? Same Teams link. Let me know if that works.</p><p>Jessica</p></body></html>""",
    expect=dict(category="interview_rescheduled", company="Brightwater Energy", interview_utc="2026-10-15T09:30:00+00:00"),
    classification=_cls(category="interview_rescheduled", company="Brightwater Energy", role_title="Reporting Analyst",
                        interview_datetime="2026-10-15T10:30:00+01:00", interview_format="video", interview_stage="first round",
                        summary="Interview moved to 15 Oct at 10:30."),
)

ASSESSMENT = dict(
    sender="no-reply@hirevue.com",
    subject="Complete your assessment for Calder Retail Group",
    received="2026-10-02T11:00:00Z",
    html="""<html><body><p>Hello Huw,</p><p>Calder Retail Group has invited you to complete an online numerical reasoning assessment as part of your application for Insight Analyst.</p>
<p>It takes about 30 minutes and must be completed by 9 October 2026.</p><p><a href="https://app.hirevue.com/candidates/assess/8f3a">Start assessment</a></p>
<p>Powered by HireVue. Privacy policy | Terms</p></body></html>""",
    expect=dict(category="assessment", company="Calder Retail Group"),
    classification=_cls(category="assessment", company="Calder Retail Group", role_title="Insight Analyst", source="HireVue",
                        summary="Online numerical reasoning assessment, due 9 Oct."),
)

NEWSLETTER = dict(
    sender="alerts@reed.co.uk",
    subject="15 new Data Analyst jobs in Manchester",
    received="2026-10-03T06:00:00Z",
    html="""<html><body><h3>Jobs picked for you</h3><ul><li>Data Analyst - Acme Ltd - £35k</li><li>BI Developer - Foo plc - £45k</li></ul>
<p>Manage your job alerts | Unsubscribe</p></body></html>""",
    expect=dict(category="other", is_job_related=False),
    classification=_cls(category="other", is_job_related=False, summary="Job alert digest.", confidence=0.9),
)

ALL = [LINKEDIN_CONFIRMATION, WORKDAY_CONFIRMATION, REJECTION, INTERVIEW_INVITE_WITH_TIME, RESCHEDULE, ASSESSMENT, NEWSLETTER]
