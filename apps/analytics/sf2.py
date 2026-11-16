import calendar
import io
import re
from collections import defaultdict
from pathlib import PurePosixPath
from zipfile import BadZipFile

from django.core.exceptions import ValidationError
from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.utils.exceptions import InvalidFileException

from apps.academic_structure.models import Section
from apps.attendance.models import AttendanceRecord
from apps.system_settings.models import SchoolProfile
from apps.virtual_classrooms.models import (
    VirtualClassroom,
    VirtualClassroomEnrollment,
)


class SF2TemplateError(Exception):
    pass


_FIELD_ALIASES = {
    "number": ("no", "number", "serialnumber"),
    "lrn": ("lrn", "learnerreferencenumber"),
    "full_name": ("name", "nameoflearner", "learnersname", "nameofstudent"),
    "last_name": ("lastname", "surname"),
    "first_name": ("firstname", "givenname"),
    "middle_initial": ("middleinitial", "middle name", "middlename"),
    "suffix": ("suffix",),
    "sex": ("sex", "gender"),
    "grade_level": ("grade", "gradelevel"),
    "section": ("section",),
    "school_year": ("schoolyear", "academicyear"),
    "month": ("month", "reportingmonth"),
    "school_name": ("schoolname", "nameofschool", "nameoftheschool"),
    "school_id": ("schoolid", "depedschoolid", "schoolnumber"),
    "school_type": ("schooltype", "schoolclassification"),
    "region": ("region", "regionname"),
    "division": ("division", "divisionname", "schoolsdivision"),
    "district": ("district", "districtname"),
    "address_line": ("schooladdress", "address"),
    "barangay": ("barangay",),
    "municipality_city": ("municipality", "city", "municipalitycity"),
    "province": ("province",),
    "postal_code": ("postalcode", "zipcode"),
    "telephone": ("telephone", "phonenumber", "contactnumber"),
    "email": ("schoolemail", "emailaddress"),
    "website": ("website", "schoolwebsite"),
    "school_head_name": ("schoolhead", "schoolheadname", "principal", "principalname"),
    "school_head_title": ("schoolheadtitle", "principalposition"),
    "present_total": ("present", "presenttotal", "dayspresent"),
    "absent_total": ("absent", "absenttotal", "daysabsent"),
    "late_total": ("late", "latetotal", "dayslate", "tardy", "tardytotal"),
    "excused_total": ("excused", "excusedtotal", "daysexcused"),
    "total": ("total", "totaldays"),
}
_METADATA_FIELDS = {
    "school_year", "month", "grade_level", "section", "school_name", "school_id",
    "school_type", "region", "division", "district", "address_line", "barangay",
    "municipality_city", "province", "postal_code", "telephone", "email", "website",
    "school_head_name", "school_head_title",
}


def _normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _field_from_header(value):
    normalized = _normalize(value)
    if normalized.isdigit() and 1 <= int(normalized) <= 31:
        return f"day_{int(normalized)}"
    day_match = re.fullmatch(r"(?:day)?0?([1-9]|[12][0-9]|3[01])", normalized)
    if day_match:
        return f"day_{int(day_match.group(1))}"
    for field, aliases in _FIELD_ALIASES.items():
        if normalized in {_normalize(alias) for alias in aliases}:
            return field
    if "learner" in normalized and "name" in normalized:
        return "full_name"
    if "student" in normalized and "name" in normalized:
        return "full_name"
    return None


def validate_sf2_template_file(upload, field_mappings=None):
    suffix = PurePosixPath(upload.name).suffix.lower()
    if suffix not in {".xlsx", ".xlsm"}:
        raise ValidationError("SF2 templates must be Excel workbooks (.xlsx or .xlsm).")
    try:
        upload.seek(0)
        workbook = load_workbook(upload, keep_vba=(suffix == ".xlsm"))
        if not workbook.sheetnames:
            raise ValidationError("The Excel template must contain at least one worksheet.")
        if field_mappings is not None:
            config, columns, _cells = _mapping_config(field_mappings)
            worksheet = workbook[workbook.sheetnames[0]]
            standard_layout = _standard_sf2_layout(worksheet) if not columns else None
            if standard_layout is None:
                _column_fields(worksheet, _header_rows(worksheet, config, columns), columns)
        workbook.close()
    except SF2TemplateError as exc:
        raise ValidationError(str(exc)) from exc
    except (BadZipFile, InvalidFileException, OSError, ValueError) as exc:
        raise ValidationError("The uploaded file is not a readable Excel workbook.") from exc
    finally:
        upload.seek(0)


def _mapping_config(field_mappings):
    if not isinstance(field_mappings, dict):
        raise SF2TemplateError("SF2 field mappings must be a JSON object.")
    config = field_mappings
    columns = config.get("columns") or {}
    cells = config.get("cells") or {}
    if not isinstance(columns, dict):
        raise SF2TemplateError("Template columns must be a JSON object.")
    if not isinstance(cells, dict):
        raise SF2TemplateError("Template cells must be a JSON object.")
    if any(field not in _METADATA_FIELDS for field in cells):
        raise SF2TemplateError(
            "Template cell mappings may only target school_year, month, grade_level, or section."
        )
    columns.update(
        {
            key: value
            for key, value in config.items()
            if key not in {"columns", "cells", "header_row", "header_rows", "first_data_row", "row_limit"}
            and isinstance(value, str)
        }
    )
    return config, columns, cells


def _header_rows(worksheet, config, columns):
    explicit = config.get("header_rows")
    if explicit is None and config.get("header_row") is not None:
        explicit = [config["header_row"]]
    if explicit:
        if not isinstance(explicit, (list, tuple)):
            raise SF2TemplateError("Template header_rows must be a list of row numbers.")
        try:
            rows = [int(value) for value in explicit]
        except (TypeError, ValueError) as exc:
            raise SF2TemplateError("Template header_rows must contain row numbers.") from exc
        if any(row < 1 or row > worksheet.max_row for row in rows):
            raise SF2TemplateError("A configured template header row is outside the worksheet.")
        return rows

    best_rows = []
    best_score = 0
    for row_number in range(1, min(worksheet.max_row, 50) + 1):
        score = 0
        for cell in worksheet[row_number]:
            if _field_from_header(cell.value):
                score += 1
            if _normalize(cell.value) in {_normalize(key) for key in columns}:
                score += 2
        if score > best_score:
            best_rows = [row_number]
            best_score = score
    if not best_rows:
        raise SF2TemplateError(
            "Could not identify the SF2 column headings. Add header_row and field_mappings to the template."
        )
    return best_rows


def _column_fields(worksheet, header_rows, columns):
    configured = {_normalize(key): _normalize_field(value) for key, value in columns.items()}
    fields = {}
    for row_number in header_rows:
        for cell in worksheet[row_number]:
            header = _normalize(cell.value)
            field = configured.get(header) or _field_from_header(cell.value)
            if field:
                fields[cell.column] = field

    if not any(field == "full_name" for field in fields.values()) and not {
        "first_name",
        "last_name",
    }.issubset(set(fields.values())):
        raise SF2TemplateError(
            "Map a learner name column (or both first and last name columns) in the SF2 template."
        )
    if not any(field.startswith("day_") for field in fields.values()):
        raise SF2TemplateError(
            "Map at least one numbered attendance-day column in the SF2 template."
        )
    return fields


def _standard_sf2_layout(worksheet):
    """Infer the official DepEd SF2 merged-header layout without manual maps."""
    merged = list(getattr(worksheet, "merged_cells", ()).ranges) if hasattr(
        worksheet, "merged_cells"
    ) else []
    name_header = None
    date_header = None
    for cell in worksheet.iter_rows(min_row=1, max_row=min(worksheet.max_row, 50)):
        for item in cell:
            value = item.value
            if not isinstance(value, str):
                continue
            normalized = _normalize(value)
            if name_header is None and _field_from_header(value) == "full_name":
                name_header = next(
                    (
                        area for area in merged
                        if area.min_row == item.row and area.min_col == item.column
                    ),
                    None,
                )
            if (
                date_header is None
                and "date" in normalized
                and "day" in normalized
                and "row" in normalized
            ):
                date_header = next(
                    (
                        area for area in merged
                        if area.min_row == item.row and area.min_col == item.column
                    ),
                    None,
                )
    if not name_header or not date_header or name_header.max_col <= name_header.min_col:
        return None

    first_data_row = name_header.max_row + 1
    name_data = next(
        (
            area for area in merged
            if area.min_row == first_data_row
            and area.max_col > area.min_col
            and area.min_col > name_header.min_col
            and area.max_col <= name_header.max_col
        ),
        None,
    )
    if name_data is None:
        return None

    row_limit = first_data_row - 1
    data_merges = {
        area.min_row: area
        for area in merged
        if area.min_col == name_data.min_col and area.max_col == name_data.max_col
    }
    while row_limit + 1 in data_merges:
        row_limit += 1

    first_day_column = date_header.min_col
    last_day_column = min(date_header.max_col, first_day_column + 30)
    daily_columns = list(range(first_day_column, last_day_column + 1))
    fields = {name_header.min_col: "number", name_data.min_col: "full_name"}
    fields.update(
        {column: f"day_{index}" for index, column in enumerate(daily_columns, 1)}
    )
    for row_number in range(date_header.min_row, first_data_row):
        for cell in worksheet[row_number]:
            field = _field_from_header(cell.value)
            if field in {
                "present_total", "absent_total", "late_total",
                "excused_total", "total",
            }:
                fields[cell.column] = field

    if not any(field == "full_name" for field in fields.values()):
        return None
    return {
        "fields": fields,
        "first_data_row": first_data_row,
        "row_limit": row_limit,
        "header_rows": [name_header.min_row],
        "metadata_row_limit": min(name_header.min_row, date_header.min_row) - 1,
        "daily_columns": daily_columns,
        "date_row": date_header.min_row + 1,
        "weekday_row": date_header.min_row + 2,
    }


def _normalize_field(value):
    normalized = _normalize(value)
    if normalized in {"name", "fullname", "learnername", "studentname"}:
        return "full_name"
    if normalized in {"day", "attendance"}:
        raise SF2TemplateError("Map attendance headings to numbered day fields such as day_1.")
    if normalized.startswith("day") and normalized[3:].isdigit():
        day = int(normalized[3:])
        if 1 <= day <= 31:
            return f"day_{day}"
    for field in _FIELD_ALIASES:
        if _normalize(field) == normalized:
            return field
    raise SF2TemplateError(f"Unknown SF2 template data field: {value}.")


def _sheet_name_base(section, page=None):
    base = f"G{section.grade_level} {section.name}"
    base = re.sub(r"[\[\]:*?/\\]", "-", base).strip("'") or "SF2"
    if page is not None:
        tail = f" ({page})"
        base = f"{base[:31 - len(tail)]}{tail}"
    else:
        base = base[:31]
    return base or "SF2"


def _sheet_name(section, used_names, page=None):
    base = _sheet_name_base(section, page)
    candidate = base
    suffix = 2
    while candidate in used_names:
        tail = f" ({suffix})"
        candidate = f"{base[:31 - len(tail)]}{tail}"
        suffix += 1
    used_names.add(candidate)
    return candidate


def _attendance_code(statuses):
    if AttendanceRecord.PRESENT_ON_TIME in statuses:
        return "/"
    if AttendanceRecord.PRESENT_LATE in statuses:
        return "L"
    if AttendanceRecord.ABSENT_EXCUSED in statuses:
        return "E"
    if AttendanceRecord.ABSENT_UNEXCUSED in statuses:
        return "-"
    return ""


def _student_rows(classrooms, section, reporting_month, weekdays_only=False):
    first_day = reporting_month.replace(day=1)
    last_day = reporting_month.replace(
        day=calendar.monthrange(reporting_month.year, reporting_month.month)[1]
    )
    section_classrooms = classrooms.filter(section=section)
    enrollments = (
        VirtualClassroomEnrollment.objects.filter(
            virtual_classroom__in=section_classrooms,
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        )
        .select_related("student")
        .order_by("student__last_name", "student__first_name", "student_id")
    )
    unique_students = {}
    for enrollment in enrollments:
        unique_students.setdefault(enrollment.student_id, enrollment.student)
    student_ids = list(unique_students)
    records = AttendanceRecord.objects.filter(
        student_id__in=student_ids,
        session__virtual_classroom__in=section_classrooms,
        session__session_date__range=(first_day, last_day),
    ).values_list("student_id", "session__session_date", "status")
    statuses = defaultdict(set)
    for student_id, session_date, status in records:
        statuses[(student_id, session_date.day)].add(status)

    rows = []
    month_dates = [
        reporting_month.replace(day=day)
        for day in range(1, last_day.day + 1)
    ]
    if weekdays_only:
        month_dates = [day for day in month_dates if day.weekday() < 5]
    for student_id, student in unique_students.items():
        days = {
            f"day_{index}": _attendance_code(statuses[(student_id, day.day)])
            for index, day in enumerate(month_dates, 1)
        }
        markers = list(days.values())
        rows.append(
            {
                **days,
                "number": len(rows) + 1,
                "lrn": student.lrn,
                "full_name": ", ".join(
                    part for part in (
                        student.last_name,
                        " ".join(
                            part for part in (
                                student.first_name,
                                student.middle_initial,
                            )
                            if part
                        ),
                        student.suffix,
                    )
                    if part
                ),
                "last_name": student.last_name,
                "first_name": student.first_name,
                "middle_initial": student.middle_initial,
                "suffix": student.suffix,
                "sex": student.sex,
                "grade_level": section.grade_level,
                "section": section.name,
                "school_year": section.academic_year.label,
                "month": reporting_month.strftime("%B %Y"),
                "present_total": sum(marker in {"/", "L"} for marker in markers),
                "absent_total": sum(marker in {"-", "E"} for marker in markers),
                "late_total": markers.count("L"),
                "excused_total": markers.count("E"),
                "total": sum(bool(marker) for marker in markers),
            }
        )
    return rows


def _write_metadata(worksheet, metadata, cells, metadata_row_limit):
    for field, address in cells.items():
        if field not in metadata or not isinstance(address, str) or not address:
            raise SF2TemplateError(f"Invalid SF2 metadata cell mapping for {field}.")
        try:
            worksheet[address] = metadata[field]
        except (AttributeError, TypeError, ValueError, KeyError) as exc:
            raise SF2TemplateError(f"Invalid SF2 metadata cell address: {address}.") from exc

    if metadata_row_limit < 1:
        return
    aliases = {
        "school_year": ("schoolyear", "academicyear"),
        "month": ("month", "reportingmonth", "reportforthemonthof"),
        "grade_level": ("grade", "gradelevel"),
        "section": ("section",),
        **{
            field: labels
            for field, labels in _FIELD_ALIASES.items()
            if field in _METADATA_FIELDS
        },
    }
    for row in worksheet.iter_rows(
        min_row=1,
        max_row=min(worksheet.max_row, metadata_row_limit, 30),
    ):
        for cell in row:
            if not isinstance(cell.value, str):
                continue
            normalized = _normalize(cell.value)
            field = next(
                (
                    candidate
                    for candidate, labels in aliases.items()
                    if candidate not in cells
                    and any(normalized.startswith(_normalize(label)) for label in labels)
                ),
                None,
            )
            if not field:
                continue
            for column in range(cell.column + 1, worksheet.max_column + 1):
                value_cell = worksheet.cell(row=cell.row, column=column)
                if not isinstance(value_cell, MergedCell):
                    value_cell.value = metadata[field]
                    break


def _data_row_bounds(worksheet, config, column_config, standard_layout):
    headers = (
        standard_layout["header_rows"]
        if standard_layout
        else _header_rows(worksheet, config, column_config)
    )
    first_data_row = config.get("first_data_row") or (
        standard_layout.get("first_data_row") if standard_layout else None
    )
    if first_data_row is None:
        first_data_row = max(headers) + 1
    try:
        first_data_row = int(first_data_row)
    except (TypeError, ValueError) as exc:
        raise SF2TemplateError("Template first_data_row must be a row number.") from exc

    row_limit = config.get("row_limit") or (
        standard_layout.get("row_limit") if standard_layout else None
    )
    try:
        row_limit = int(row_limit) if row_limit is not None else worksheet.max_row
    except (TypeError, ValueError) as exc:
        raise SF2TemplateError("Template row_limit must be a row number.") from exc
    if first_data_row < 1 or row_limit < first_data_row:
        raise SF2TemplateError("The template data rows are configured incorrectly.")
    return first_data_row, row_limit


def _fill_sheet(
    worksheet,
    rows,
    section,
    reporting_month,
    config,
    column_config,
    cells,
    academic_year,
    standard_layout=None,
):
    if standard_layout is None and not column_config:
        standard_layout = _standard_sf2_layout(worksheet)
    headers = (
        standard_layout["header_rows"]
        if standard_layout
        else _header_rows(worksheet, config, column_config)
    )
    fields_by_column = (
        standard_layout["fields"]
        if standard_layout
        else _column_fields(worksheet, headers, column_config)
    )
    first_data_row, row_limit = _data_row_bounds(
        worksheet, config, column_config, standard_layout
    )
    if len(rows) > row_limit - first_data_row + 1:
        raise SF2TemplateError("SF2 learner rows exceed the template page capacity.")

    metadata = {
        "school_year": academic_year.label,
        "month": reporting_month.strftime("%B %Y"),
        "grade_level": section.grade_level,
        "section": section.name,
    }
    profile = SchoolProfile.get_solo()
    metadata.update({
        "school_name": profile.school_name,
        "school_id": profile.school_id,
        "school_type": profile.school_type,
        "region": profile.region,
        "division": profile.division,
        "district": profile.district,
        "address_line": profile.address_line,
        "barangay": profile.barangay,
        "municipality_city": profile.municipality_city,
        "province": profile.province,
        "postal_code": profile.postal_code,
        "telephone": profile.telephone,
        "email": profile.email,
        "website": profile.website,
        "school_head_name": profile.school_head_name,
        "school_head_title": profile.school_head_title,
    })
    metadata_limit = (
        standard_layout["metadata_row_limit"]
        if standard_layout
        else min(headers) - 1
    )
    _write_metadata(worksheet, metadata, cells, metadata_limit)

    if standard_layout:
        month_dates = [
            reporting_month.replace(day=day)
            for day in range(1, calendar.monthrange(reporting_month.year, reporting_month.month)[1] + 1)
        ]
        month_dates = [date for date in month_dates if date.weekday() < 5]
        weekday_labels = {0: "M", 1: "T", 2: "W", 3: "TH", 4: "F"}
        for index, column in enumerate(standard_layout["daily_columns"]):
            current_date = month_dates[index] if index < len(month_dates) else None
            worksheet.cell(row=standard_layout["date_row"], column=column).value = (
                current_date.day if current_date else None
            )
            worksheet.cell(row=standard_layout["weekday_row"], column=column).value = (
                weekday_labels[current_date.weekday()] if current_date else None
            )

    for offset, row in enumerate(rows):
        row_number = first_data_row + offset
        for column, field in fields_by_column.items():
            worksheet.cell(row=row_number, column=column).value = row.get(field, "")


def build_sf2_workbook(template, academic_year, reporting_month, section_ids=None):
    suffix = PurePosixPath(template.template_file.name).suffix.lower()
    if suffix not in {".xlsx", ".xlsm"}:
        raise SF2TemplateError("The active SF2 template must be an .xlsx or .xlsm workbook.")
    try:
        with template.template_file.open("rb") as source:
            workbook = load_workbook(source, keep_vba=(suffix == ".xlsm"))
    except (BadZipFile, InvalidFileException, OSError, ValueError) as exc:
        raise SF2TemplateError("The active SF2 template could not be opened.") from exc

    config, column_config, cells = _mapping_config(template.field_mappings)
    classrooms = VirtualClassroom.objects.filter(
        academic_year=academic_year,
        status=VirtualClassroom.STATUS_ACTIVE,
    ).select_related("section")
    if section_ids is not None:
        classrooms = classrooms.filter(section_id__in=section_ids)
    base_sheet = workbook.active
    standard_layout = _standard_sf2_layout(base_sheet) if not column_config else None
    section_rows = []
    for section in Section.objects.filter(
        pk__in=classrooms.values_list("section_id", flat=True).distinct()
    ).order_by("grade_level", "name"):
        rows = _student_rows(
            classrooms,
            section,
            reporting_month,
            weekdays_only=standard_layout is not None,
        )
        if rows:
            section_rows.append((section, rows))
    if not section_rows:
        raise SF2TemplateError("There are no active classroom learners for this SF2 report.")

    first_data_row, row_limit = _data_row_bounds(
        base_sheet, config, column_config, standard_layout
    )
    page_capacity = row_limit - first_data_row + 1
    report_pages = []
    for section, rows in section_rows:
        page_count = (len(rows) + page_capacity - 1) // page_capacity
        for page_index in range(page_count):
            start = page_index * page_capacity
            report_pages.append((
                section,
                rows[start:start + page_capacity],
                page_index + 1 if page_count > 1 else None,
            ))

    sheets = [base_sheet]
    for _section, _rows, _page in report_pages[1:]:
        sheets.append(workbook.copy_worksheet(base_sheet))
    used_names = set()
    for worksheet, (section, rows, page) in zip(sheets, report_pages):
        worksheet.title = _sheet_name(section, used_names, page)
        _fill_sheet(
            worksheet,
            rows,
            section,
            reporting_month,
            config,
            column_config,
            cells,
            academic_year,
            standard_layout=standard_layout,
        )

    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    output.seek(0)
    return output
