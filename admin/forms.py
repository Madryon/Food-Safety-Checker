from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField, IntegerField, TextAreaField, SelectField, HiddenField
from wtforms.validators import DataRequired, Length, Email, Optional, EqualTo, NumberRange, ValidationError
from auth.forms import validate_password_policy


class AdminLoginForm(FlaskForm):
    username_or_email = StringField(
        "Administrator ID or Email",
        validators=[DataRequired(message="Admin username or email is required.")]
    )
    password = PasswordField(
        "Admin Password",
        validators=[DataRequired(message="Password is required.")]
    )
    totp_code = StringField(
        "2FA Code (if enabled)",
        validators=[Optional(), Length(min=6, max=6, message="Authenticator code must be 6 digits.")]
    )
    submit = SubmitField("Sign In to Console")


class AdminRegisterForm(FlaskForm):
    username = StringField(
        "Admin Username",
        validators=[
            DataRequired(message="Username is required."),
            Length(min=3, max=80, message="Username must be between 3 and 80 characters.")
        ]
    )
    email = StringField(
        "Admin Email",
        validators=[DataRequired(message="Valid email is required."), Email(check_deliverability=False), Length(max=120)]
    )
    name = StringField(
        "Administrator Full Name",
        validators=[DataRequired(message="Full name is required."), Length(min=2, max=150)]
    )
    password = PasswordField(
        "Password",
        validators=[DataRequired(message="Password is required."), validate_password_policy]
    )
    confirm_password = PasswordField(
        "Confirm Password",
        validators=[
            DataRequired(message="Please confirm password."),
            EqualTo("password", message="Passwords must match.")
        ]
    )
    invite_key = StringField(
        "Admin Invite Key",
        validators=[DataRequired(message="Developer-issued Admin Invite Key is required.")]
    )
    submit = SubmitField("Register Administrator Account")


class AdminSettingsForm(FlaskForm):
    maintenance_mode = BooleanField("Maintenance Mode (Only admins can access app)")
    registration_enabled = BooleanField("Allow Public User Registration")
    announcement_banner = StringField("Global Announcement Banner Text", validators=[Optional(), Length(max=255)])
    daily_scan_limit_anon = IntegerField(
        "Daily Scan Limit per Anonymous IP",
        validators=[DataRequired(), NumberRange(min=1, max=1000)]
    )
    daily_scan_limit_user = IntegerField(
        "Daily Scan Limit per Signed-in User",
        validators=[DataRequired(), NumberRange(min=1, max=5000)]
    )
    submit = SubmitField("Save Application Settings")


class AdminExportForm(FlaskForm):
    export_type = SelectField(
        "Data Dataset",
        choices=[
            ("users", "Registered Users"),
            ("scans", "Food Safety Scans"),
            ("login_events", "Authentication & Login Events"),
            ("error_logs", "System Error Logs"),
        ],
        validators=[DataRequired()]
    )
    date_from = StringField("From Date (YYYY-MM-DD)", validators=[Optional()])
    date_to = StringField("To Date (YYYY-MM-DD)", validators=[Optional()])
    submit = SubmitField("Download CSV Export")
