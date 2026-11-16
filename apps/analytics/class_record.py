"""Fill an administrator supplied DepEd class record workbook."""

from io import BytesIO
from pathlib import PurePosixPath

from django.core.exceptions import ValidationError
from openpyxl import load_workbook
from openpyxl.utils.cell import column_index_from_string

from apps.assessments.models import Submission


ALLOWED_CELL_FIELDS = {
    "school_year", "grade_level", "section", "teacher", "subject", "term"
}


def validate_class_record_template_file(upload, mappings=None):
    name = getattr(upload, "name", "")
    if PurePosixPath(name).suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValidationError("Class record templates must be .xlsx or .xlsm Excel workbooks.")
    mappings = mappings or {}
    if not isinstance(mappings, dict):
        raise ValidationError("Field mappings must be a JSON object.")
    try:
        upload.seek(0)
        workbook = load_workbook(upload, read_only=True, data_only=False)
        sheet_name = mappings.get("sheet_name")
        if sheet_name and sheet_name not in workbook.sheetnames:
            raise ValidationError("The configured worksheet was not found in the workbook.")
        if not workbook.sheetnames:
            raise ValidationError("The workbook has no worksheets.")
        workbook.close()
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError("Could not read this Excel workbook: {}".format(exc))
    finally:
        upload.seek(0)

    name_column = mappings.get("name_column")
    if name_column is not None:
        _column_index(name_column)
    columns = mappings.get("assessment_columns", [])
    if not isinstance(columns, (list, dict)):
        raise ValidationError("assessment_columns must be a list of columns or a title-to-column object.")
    for column in (columns.values() if isinstance(columns, dict) else columns):
        _column_index(column)
    cells = mappings.get("cells", {})
    if not isinstance(cells, dict) or set(cells) - ALLOWED_CELL_FIELDS:
        raise ValidationError("cells may map school_year, grade_level, section, teacher, subject, and term.")
    for coordinate in cells.values():
        if not isinstance(coordinate, str) or not coordinate.strip():
            raise ValidationError("Each metadata cell must be an Excel cell address such as B3.")
    for key in ("first_data_row", "row_limit", "header_row"):
        if key in mappings and (not isinstance(mappings[key], int) or mappings[key] < 1):
            raise ValidationError("{} must be a positive row number.".format(key))
    gender_rows = mappings.get("gender_rows", {})
    if not isinstance(gender_rows, dict):
        raise ValidationError("gender_rows must map M and F to first_data_row and row_limit ranges.")
    for gender, bounds in gender_rows.items():
        if gender.upper() not in {"M", "F", "MALE", "FEMALE"} or not isinstance(bounds, dict):
            raise ValidationError("gender_rows must map M and F to row ranges.")
        if any(not isinstance(bounds.get(key), int) or bounds[key] < 1 for key in ("first_data_row", "row_limit")):
            raise ValidationError("Each gender row range needs positive first_data_row and row_limit values.")
        if bounds["row_limit"] < bounds["first_data_row"]:
            raise ValidationError("Each gender row_limit must be on or after its first_data_row.")
    return True


def _column_index(value):
    try:
        if isinstance(value, int):
            if value < 1:
                raise ValueError
            return value
        return column_index_from_string(str(value).strip().upper())
    except (TypeError, ValueError):
        raise ValidationError("Excel columns must be letters such as B or column numbers such as 2.")


def _clean_number(value):
    if value is None:
        return None
    value = float(value)
    return int(value) if value.is_integer() else value


def build_class_record_workbook(template, classroom):
    """Return the filled workbook, preserving the uploaded sheet and formatting.

    Mapping example::
        {"sheet_name":"Term 1", "first_data_row":14, "row_limit":47,
         "name_column":"B", "assessment_columns":["C","D","E"],
         "cells":{"school_year":"J2", "grade_level":"C5", "section":"C6",
                  "teacher":"F5", "subject":"J5", "term":"A1"}}
    """
    mappings = template.field_mappings or {}
    validate_class_record_template_file(template.template_file, mappings)
    suffix = PurePosixPath(template.template_file.name).suffix.lower()
    template.template_file.open("rb")
    try:
        workbook = load_workbook(
            template.template_file,
            keep_vba=suffix == ".xlsm",
            data_only=False,
        )
    finally:
        template.template_file.close()
    sheet = workbook[mappings["sheet_name"]] if mappings.get("sheet_name") else workbook[workbook.sheetnames[0]]

    assignments = list(
        classroom.assessment_assignments.select_related("assessment")
        .order_by("assigned_at", "pk")
    )
    enrollments = list(
        classroom.enrollments.filter(status="active")
        .select_related("student", "student__user")
        .order_by("student__last_name", "student__first_name", "student__pk")
    )
    first_row = int(mappings.get("first_data_row", 2))
    row_limit = int(mappings.get("row_limit", first_row + max(len(enrollments), 1) - 1))
    name_column = _column_index(mappings.get("name_column", "A"))
    if first_row < 1 or row_limit < first_row:
        workbook.close()
        raise ValidationError("first_data_row and row_limit do not define a valid learner row range.")
    gender_rows = {key.upper(): value for key, value in (mappings.get("gender_rows") or {}).items()}
    student_row_map = {}
    if gender_rows:
        gender_aliases = {"M": ("M", "MALE"), "F": ("F", "FEMALE")}
        for gender_key, aliases in gender_aliases.items():
            bounds = next((gender_rows[key] for key in aliases if key in gender_rows), None)
            students_for_gender = [
                enrollment for enrollment in enrollments
                if str(enrollment.student.sex or "").upper() in aliases
            ]
            if students_for_gender and bounds is None:
                workbook.close()
                raise ValidationError("The official template is missing a {} learner row range.".format("male" if gender_key == "M" else "female"))
            if bounds is None:
                continue
            capacity = bounds["row_limit"] - bounds["first_data_row"] + 1
            if len(students_for_gender) > capacity:
                workbook.close()
                raise ValidationError("The uploaded template does not have enough {} learner rows for this class.".format("male" if gender_key == "M" else "female"))
            student_row_map.update({
                enrollment.student_id: bounds["first_data_row"] + index
                for index, enrollment in enumerate(students_for_gender)
            })
        unknown_gender = [en for en in enrollments if str(en.student.sex or "").upper() not in {"M", "MALE", "F", "FEMALE"}]
        if unknown_gender:
            workbook.close()
            raise ValidationError("The template has separate male/female rows but one or more learners have no recognized sex value.")
    else:
        if len(enrollments) > row_limit - first_row + 1:
            workbook.close()
            raise ValidationError("The uploaded template does not have enough mapped learner rows for this class.")
        student_row_map = {enrollment.student_id: first_row + offset for offset, enrollment in enumerate(enrollments)}

    configured_columns = mappings.get("assessment_columns", [])
    if isinstance(configured_columns, dict):
        column_for_title = {str(title).strip().casefold(): _column_index(column) for title, column in configured_columns.items()}
        sequential_columns = []
    else:
        column_for_title = {}
        sequential_columns = [_column_index(column) for column in configured_columns]
    header_row = int(mappings.get("header_row", max(1, first_row - 1)))
    for index, assignment in enumerate(assignments):
        title = (assignment.assessment.title or "Assessment").strip()
        if title.casefold() in column_for_title:
            continue
        if index < len(sequential_columns):
            column_for_title[title.casefold()] = sequential_columns[index]
            continue
        found = None
        for cell in sheet[header_row]:
            if str(cell.value or "").strip().casefold() == title.casefold():
                found = cell.column
                break
        if found:
            column_for_title[title.casefold()] = found
        else:
            workbook.close()
            raise ValidationError(
                'No score column is mapped for "{}". Add it to the admin template field mappings.'.format(title)
            )

    values = {
        "school_year": classroom.academic_year.label if classroom.academic_year_id else "",
        "grade_level": classroom.subject.grade_level if classroom.subject_id else "",
        "section": classroom.section.name if classroom.section_id else "",
        "teacher": classroom.teacher.get_full_name() if classroom.teacher_id else "",
        "subject": classroom.subject.name if classroom.subject_id else "",
        "term": classroom.grading_period or "",
    }
    try:
        for field, coordinate in (mappings.get("cells") or {}).items():
            sheet[coordinate] = values[field]
    except (KeyError, AttributeError, ValueError) as exc:
        workbook.close()
        raise ValidationError("A metadata cell mapping is invalid or points inside a merged range: {}".format(exc))

    submissions = {
        (submission.student_id, submission.assessment_assignment_id): submission
        for submission in Submission.objects.filter(
            assessment_assignment__virtual_classroom=classroom
        ).only("student_id", "assessment_assignment_id", "total_score", "grading_status")
    }
    for enrollment in enrollments:
        row = student_row_map[enrollment.student_id]
        student = enrollment.student
        full_name = " ".join(filter(None, [student.first_name, student.middle_initial, student.last_name, student.suffix]))
        sheet.cell(row=row, column=name_column, value=full_name)
        for assignment in assignments:
            submission = submissions.get((student.pk, assignment.pk))
            if not submission or submission.total_score is None or submission.grading_status != Submission.GRADING_COMPLETE:
                continue
            column = column_for_title[(assignment.assessment.title or "Assessment").strip().casefold()]
            sheet.cell(row=row, column=column, value=_clean_number(submission.total_score))

    output = BytesIO()
    workbook.save(output)
    workbook.close()
    output.seek(0)
    return output
