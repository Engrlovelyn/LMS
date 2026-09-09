from django.utils import timezone

from apps.academic_structure.models import AcademicYear
from apps.accounts.decorators import perm_required
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from .models import RoleAccessReview
from .forms import RoleAccessReviewForm


@perm_required("governance.conduct_annual_role_access_review")
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
        completed_at__year=now.year
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
def review_complete(request, pk):
    if request.method != "POST":
        return redirect("governance:review_detail", pk=pk)

    review = get_object_or_404(RoleAccessReview, pk=pk)
    duplicate = RoleAccessReview.objects.filter(academic_year=review.academic_year).exclude(pk=review.pk).exists()
    if duplicate:
        messages.error(
            request,
            f"A review was already completed for {review.academic_year}. "
            "Only one completed review is allowed per academic year.",
        )
        return redirect("governance:review_list")

    messages.success(request, "Review marked as completed.")
    return redirect("governance:review_detail", pk=review.pk)


@perm_required("governance.conduct_annual_role_access_review")
def review_detail(request, pk):
    review = get_object_or_404(
        RoleAccessReview.objects.select_related("academic_year", "conducted_by"),
        pk=pk,
    )
    context = {
        "review": review,
        "is_completed": review.completed_at is not None,
    }
    return render(request, "governance/review_detail.html", context)
