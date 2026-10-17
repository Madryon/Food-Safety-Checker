import re
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField
from wtforms.validators import DataRequired, Length, Email, Optional, EqualTo, ValidationError


def validate_password_policy(form, field):
    """Enforce at least 8 characters, at least 1 letter, and at least 1 digit."""
    pwd = field.data or ""
    if len(pwd) < 8:
        raise ValidationError("Password must be at least 8 characters long.")
    if not re.search(r"[A-Za-z]", pwd):
        raise ValidationError("Password must contain at least one letter.")
    if not re.search(r"\d", pwd):
        raise ValidationError("Password must contain at least one number.")


class LoginForm(FlaskForm):
    username_or_email = StringField(
        "Username or Email",
        validators=[DataRequired(message="Username or Email is required."), Length(min=2, max=120)]
    )
    password = PasswordField(
        "Password",
        validators=[DataRequired(message="Password is required.")]
    )
    remember_me = BooleanField("Remember Me")
    submit = SubmitField("Sign In")


class RegisterForm(FlaskForm):
    username = StringField(
        "Username",
        validators=[
            DataRequired(message="Username is required."),
            Length(min=3, max=80, message="Username must be between 3 and 80 characters.")
        ]
    )
    email = StringField(
        "Email (optional)",
        validators=[Optional(), Email(check_deliverability=False, message="Please enter a valid email address."), Length(max=120)]
    )
    name = StringField(
        "Full Name",
        validators=[DataRequired(message="Your name is required."), Length(min=2, max=150)]
    )
    password = PasswordField(
        "Password",
        validators=[DataRequired(message="Password is required."), validate_password_policy]
    )
    confirm_password = PasswordField(
        "Confirm Password",
        validators=[
            DataRequired(message="Please confirm your password."),
            EqualTo("password", message="Passwords must match.")
        ]
    )
    submit = SubmitField("Create Account")


class DeleteAccountForm(FlaskForm):
    password = PasswordField(
        "Current Password",
        validators=[DataRequired(message="Please enter your password to confirm deletion.")]
    )
    confirm_text = StringField(
        "Type 'DELETE' to confirm",
        validators=[DataRequired()]
    )
    submit = SubmitField("Permanently Delete Account")

    def validate_confirm_text(self, field):
        if field.data != "DELETE":
            raise ValidationError("You must type exactly 'DELETE' to confirm.")
