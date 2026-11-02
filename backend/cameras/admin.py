from django.contrib import admin, messages
from django.contrib.gis.admin import GISModelAdmin
from django.utils.html import format_html

from . import publish
from .images import process_upload
from .models import Camera, CameraPhoto, CameraStatus, PhotoStatus, SourceSync


class CameraPhotoInline(admin.TabularInline):
    model = CameraPhoto
    extra = 0
    fields = ("preview", "image", "photo_type", "status", "caption", "submitted_at")
    readonly_fields = ("preview", "submitted_at")

    @admin.display(description="Preview")
    def preview(self, obj):
        if not obj.image:
            return ""
        return format_html(
            '<a href="{0}" target="_blank"><img src="{0}" style="max-height:120px;max-width:180px"></a>',
            obj.image.url,
        )


@admin.register(Camera)
class CameraAdmin(GISModelAdmin):
    gis_widget_kwargs = {"attrs": {"default_lon": -90.07, "default_lat": 29.95, "default_zoom": 12}}
    list_display = (
        "short_id",
        "camera_type",
        "status",
        "label",
        "facial_recognition",
        "source",
        "photo_count",
        "reported_at",
    )
    list_filter = ("status", "camera_type", "source", "facial_recognition", "mount")
    search_fields = (
        "host_business",
        "cross_street",
        "street_address",
        "operator",
        "source_ref",
        "admin_notes",
    )
    readonly_fields = ("id", "created_at", "updated_at", "vetted_at", "vetted_by", "osm_pushed_at", "osm_id")
    date_hierarchy = "reported_at"
    ordering = ("status", "-reported_at")
    inlines = [CameraPhotoInline]
    actions = ["approve", "reject", "approve_photos", "publish_now"]
    fieldsets = (
        ("Location", {"fields": ("location", "street_address", "cross_street", "host_business")}),
        (
            "Camera",
            {
                "fields": (
                    "camera_type",
                    "operator",
                    "manufacturer",
                    "model_name",
                    "facial_recognition",
                    "direction",
                    "mount",
                )
            },
        ),
        ("Review", {"fields": ("status", "admin_notes", "vetted_at", "vetted_by")}),
        ("Reporter (never published)", {"fields": ("reporter_contact", "reporter_notes", "reported_at")}),
        (
            "Provenance",
            {
                "classes": ("collapse",),
                "fields": (
                    "source",
                    "source_ref",
                    "osm_id",
                    "osm_pushed_at",
                    "id",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
    )

    @admin.display(description="ID")
    def short_id(self, obj):
        return str(obj.id)[:8]

    @admin.display(description="Where")
    def label(self, obj):
        return obj.cross_street or obj.street_address or obj.host_business

    @admin.display(description="Photos")
    def photo_count(self, obj):
        approved = sum(1 for p in obj.photos.all() if p.status == PhotoStatus.APPROVED)
        total = obj.photos.count()
        return f"{approved}/{total}"

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("photos")

    @admin.action(description="Approve selected cameras")
    def approve(self, request, queryset):
        n = 0
        for cam in queryset:
            cam.approve(request.user)
            n += 1
        self.message_user(request, f"Approved {n} camera(s). Run 'Publish now' to update the map.")

    @admin.action(description="Reject selected cameras")
    def reject(self, request, queryset):
        n = 0
        for cam in queryset:
            cam.reject(request.user)
            n += 1
        self.message_user(request, f"Rejected {n} camera(s).")

    @admin.action(description="Approve all pending photos on selected cameras")
    def approve_photos(self, request, queryset):
        n = 0
        for photo in CameraPhoto.objects.filter(camera__in=queryset, status=PhotoStatus.PENDING):
            photo.approve(request.user)
            n += 1
        self.message_user(request, f"Approved {n} photo(s).")

    @admin.action(description="Publish map data now")
    def publish_now(self, request, queryset):
        try:
            where = publish.publish()
        except Exception as e:
            self.message_user(request, f"Publish failed: {e}", level=messages.ERROR)
            return
        self.message_user(request, f"Published: {where}")

    def save_model(self, request, obj, form, change):
        if "status" in form.changed_data and obj.status in (CameraStatus.VETTED, CameraStatus.REJECTED):
            from django.utils import timezone

            obj.vetted_at = timezone.now()
            obj.vetted_by = request.user
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        # Admin-uploaded photos get the same EXIF stripping as public ones.
        for f in formset.forms:
            img = f.cleaned_data.get("image") if hasattr(f, "cleaned_data") else None
            if img and hasattr(img, "read") and "image" in f.changed_data:
                f.instance.image = process_upload(img, name_hint=img.name).file
        super().save_formset(request, form, formset, change)


@admin.register(CameraPhoto)
class CameraPhotoAdmin(admin.ModelAdmin):
    list_display = ("preview", "camera", "photo_type", "status", "submitted_at")
    list_filter = ("status", "photo_type")
    readonly_fields = ("preview", "submitted_at", "reviewed_at", "reviewed_by")
    actions = ["approve", "reject"]

    @admin.display(description="Preview")
    def preview(self, obj):
        if not obj.image:
            return ""
        return format_html('<img src="{}" style="max-height:80px">', obj.image.url)

    @admin.action(description="Approve selected photos")
    def approve(self, request, queryset):
        for p in queryset:
            p.approve(request.user)
        self.message_user(request, f"Approved {queryset.count()} photo(s).")

    @admin.action(description="Reject selected photos")
    def reject(self, request, queryset):
        for p in queryset:
            p.reject(request.user)
        self.message_user(request, f"Rejected {queryset.count()} photo(s).")


@admin.register(SourceSync)
class SourceSyncAdmin(admin.ModelAdmin):
    list_display = (
        "source",
        "status",
        "started_at",
        "finished_at",
        "records_seen",
        "records_created",
        "records_updated",
    )
    list_filter = ("source", "status")
    readonly_fields = [f.name for f in SourceSync._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


admin.site.site_header = "Retina admin"
admin.site.site_title = "Retina"
admin.site.index_title = "Camera review"
