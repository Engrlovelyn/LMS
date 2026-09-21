from django.utils import timezone

from apps.academic_structure.models import AcademicYear
from apps.accounts.decorators import perm_required
from django.contrib import messages
from django.contrib.auth.models import Group
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from .models import RoleAccessReview
from .forms import RoleAccessReviewForm


@perm_required("governance.retrieve_role_access_review_records")
def review_list(request):
    reviews = RoleAccessReview.objects.select_related("academic_year", "conducted_by").all()

    academic_year_id = request.GET.get("year")
    if academic_year_id:
        reviews = reviews.filter(academic_year_id=academic_year_id)

    filtered_year = AcademicYear.objects.filter(pk=academic_year_id).first() if academic_year_id else None
    academic_years = AcademicYear.objects.all()

    total_reviews = RoleAccessReview.objects.count()
    now = timezone.now()
    completed_this_year = RoleAccessReview.objects.filter(
        completed_at__year=now.year,
        completed_at__isnull=False,
    ).count()

    context = {
        "reviews": reviews,
        "academic_years": academic_years,
        "filtered_year": filtered_year,
        "total_reviews": total_reviews,
        "completed_this_year": completed_this_year,
    }
    return render(request, "governance/review_list.html", context)


@perm_required("governance.conduct_annual_role_access_review")
def review_add(request):
    if request.method == "POST":
        form = RoleAccessReviewForm(request.POST)
        if form.is_valid():
            academic_year = form.cleaned_data["academic_year"]
            if RoleAccessReview.objects.filter(academic_year=academic_year).exists():
                messages.error(
                    request,
                    f"A review already exists for {academic_year}. "
                    "Only one review is allowed per academic year.",
                )
                return redirect("governance:review_list")
            review = form.save(commit=False)
            review.conducted_by = request.user
            review.save()
            messages.success(request, "Review created successfully.")
            return redirect("governance:review_detail", pk=review.pk)
    else:
        form = RoleAccessReviewForm()

    return render(request, "governance/review_form.html", {"form": form, "title": "Add Role Access Review"})


@perm_required("governance.conduct_annual_role_access_review")
def review_update(request, pk):
    review = get_object_or_404(
        RoleAccessReview.objects.select_related("academic_year"),
        pk=pk,
    )
    if request.method == "POST":
        form = RoleAccessReviewForm(request.POST, instance=review)
        if form.is_valid():
            new_year = form.cleaned_data["academic_year"]
            if (
                new_year != review.academic_year
                and RoleAccessReview.objects.filter(academic_year=new_year)
                .exclude(pk=review.pk)
                .exists()
            ):
                messages.error(
                    request,
                    f"A review already exists for {new_year}. "
                    "Only one review is allowed per academic year.",
                )
                return redirect("governance:review_detail", pk=review.pk)
            form.save()
            messages.success(request, "Review updated successfully.")
            return redirect("governance:review_detail", pk=review.pk)
    else:
        form = RoleAccessReviewForm(instance=review)

    return render(
        request,
        "governance/review_form.html",
        {"form": form, "title": "Update Role Access Review", "review": review},
    )


@perm_required("governance.conduct_annual_role_access_review")
def review_complete(request, pk):
    if request.method != "POST":
        return redirect("governance:review_detail", pk=pk)

    review = get_object_or_404(RoleAccessReview, pk=pk)
    if review.is_completed:
        messages.info(request, "This review is already completed.")
        return redirect("governance:review_detail", pk=review.pk)

    duplicate = RoleAccessReview.objects.filter(
        academic_year=review.academic_year
    ).exclude(pk=review.pk).exists()
    if duplicate:
        messages.error(
            request,
            f"A review was already completed for {review.academic_year}. "
            "Only one completed review is allowed per academic year.",
        )
        return redirect("governance:review_list")

    review.completed_at = timezone.now()
    # Capture the current role group snapshot as of completion.
    review.role_snapshot = {
        group.name: group.user_set.filter(is_active=True).count()
        for group in Group.objects.all()
    }
    review.save(update_fields=["completed_at", "role_snapshot"])
    messages.success(request, "Review marked as completed.")
    return redirect("governance:review_detail", pk=review.pk)


@perm_required("governance.conduct_annual_role_access_review")
def review_delete(request, pk):
    if request.method != "POST":
        return redirect("governance:review_detail", pk=pk)
    review = get_object_or_404(RoleAccessReview, pk=pk)
    review.delete()
    messages.success(request, "Review deleted.")
    return redirect("governance:review_list")


@perm_required("governance.retrieve_role_access_review_records")
def review_detail(request, pk):
    review = get_object_or_404(
        RoleAccessReview.objects.select_related("academic_year", "conducted_by"),
        pk=pk,
    )
    context = {
        "review": review,
        "is_completed": review.is_completed,
        "role_snapshot": review.role_snapshot or {},
    }
    return render(request, "governance/review_detail.html", context)