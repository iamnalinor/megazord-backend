import logging
import uuid
from typing import Annotated

from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import File, Query, Router, UploadedFile

from accounts.models import Email
from hackathons.models import NotificationStatus
from megazord.api.codes import ERROR_CODES
from megazord.api.requests import APIRequest
from megazord.schemas import ErrorSchema, StatusSchema
from profiles.schemas import ProfileSchema
from teams.models import Team
from teams.schemas import EmailSchema, TeamSchema
from utils.notification import send_notification

from .models import Hackathon, Role
from .schemas import (
    AnalyticsSchema,
    EmailsSchema,
    HackathonCreateSchema,
    HackathonEditSchema,
    HackathonSchema,
    HackathonSummarySchema,
    NotificationStatusSchema,
)
from .services import get_emails_from_csv, make_csv

logger = logging.getLogger(__name__)

hackathon_router = Router()
my_hackathon_router = Router()


@hackathon_router.post(
    path="/",
    response={201: HackathonSchema, 401: ErrorSchema, 400: ErrorSchema},
)
async def create_hackathon(
    request: APIRequest,
    body: HackathonCreateSchema,
    image_cover: UploadedFile = File(),
    csv_emails: UploadedFile = File(default=None),
):
    user = request.user
    if not user.is_organizator:
        return 403, {
            "detail": "You are not organizator and you can't create hackathons"
        }

    # Проверка на наличие запятых в названии хакатона
    if "," in body.name:
        return 400, {"detail": "Hackathon name cannot contain commas."}

    hackathon = Hackathon(
        creator=user,
        name=body.name,
        description=body.description,
        min_participants=body.min_participants,
        max_participants=body.max_participants,
        image_cover=image_cover.read(),
    )
    await hackathon.asave()

    for role in body.roles:
        await hackathon.roles.acreate(name=role)

    participants = set(body.participants)
    if csv_emails is not None:
        csv_participants = get_emails_from_csv(file=csv_emails)
        participants |= set(csv_participants)

    for participant in participants:
        # Создание или получение объекта Email
        email_obj, created = await Email.objects.aget_or_create(email=participant)

        # Добавление email в хакатон
        await hackathon.emails.aadd(email_obj)

    return 201, await hackathon.to_entity()


@hackathon_router.post(
    path="/join", response={200: HackathonSchema, ERROR_CODES: ErrorSchema}
)
async def join_hackathon(
    request: APIRequest,
    hackathon_id: uuid.UUID,
    role_name: Annotated[str | None, Query(alias="role")] = None,
):
    user = request.user
    hackathon = await aget_object_or_404(
        Hackathon.objects.select_related("creator"),
        id=hackathon_id,
        status=Hackathon.Status.STARTED,
    )
    if not await hackathon.emails.filter(email=user.email).aexists():
        return 403, ErrorSchema(
            detail="You have not been added to the hackathon participants"
        )

    if role_name is None:
        if await hackathon.roles.acount() != 0:
            return 400, ErrorSchema(detail="Please, choice role")
    else:
        role = await aget_object_or_404(Role, hackathon=hackathon, name=role_name)
        await role.users.aadd(user, through_defaults={"hackathon": hackathon})

    await hackathon.participants.aadd(user)
    return 200, await hackathon.to_entity()


@hackathon_router.post(
    path="/{hackathon_id}/send_invites",
    response={200: StatusSchema, ERROR_CODES: ErrorSchema},
)
async def send_invites(
    request: APIRequest, hackathon_id: uuid.UUID, emails_schema: EmailsSchema
):
    hackathon = await aget_object_or_404(Hackathon, id=hackathon_id)

    if hackathon.creator_id != request.user.id:
        return 403, ErrorSchema(detail="You are not creator")

    await send_notification(
        emails=hackathon.emails.filter(email__in=emails_schema.emails),
        context={"hackathon": hackathon},
        mail_template="hackathons/mail/invitation_to_hackathon.html",
        telegram_template="hackathons/telegram/invitation_to_hackathon.html",
    )

    return 200, StatusSchema()


@hackathon_router.post(
    path="/{hackathon_id}/add_user",
    response={201: HackathonSchema, ERROR_CODES: ErrorSchema},
)
async def add_user_to_hackathon(
    request: APIRequest, hackathon_id: uuid.UUID, email_schema: EmailSchema
):
    user = request.user
    hackathon = await aget_object_or_404(
        Hackathon.objects.select_related("creator"), id=hackathon_id
    )

    if hackathon.creator != user:
        return 403, ErrorSchema(
            detail="You are not creator and you can not edit this hackathon"
        )

    if await hackathon.emails.filter(email=email_schema.email).aexists():
        return 400, ErrorSchema(detail="User already in hackathon")

    email_obj, _ = await Email.objects.aget_or_create(email=email_schema.email)
    await hackathon.emails.aadd(email_obj)

    return 201, await hackathon.to_entity()


@hackathon_router.delete(
    path="/{hackathon_id}/remove_user",
    response={200: StatusSchema, ERROR_CODES: ErrorSchema},
)
async def remove_user_from_hackathon(
    request: APIRequest, hackathon_id: uuid.UUID, email_schema: EmailSchema
):
    user = request.user
    hackathon = await aget_object_or_404(
        Hackathon.objects.select_related("creator"), id=hackathon_id
    )
    user_to_remove = await aget_object_or_404(
        Hackathon.participants, user__email=email_schema.email
    )

    if hackathon.creator != user:
        return 403, ErrorSchema(
            detail="You are not creator and you can not edit this hackathon"
        )

    if user_to_remove == user:
        return 400, ErrorSchema(detail="You can not remove self")

    await hackathon.participants.aremove(user_to_remove)

    await send_notification(
        users=user_to_remove,
        context={"hackathon": hackathon},
        mail_template="hackathons/mail/user_kicked.html",
        telegram_template="hackathons/telegram/user_kicked.html",
    )

    return 200, await hackathon.to_entity()


@hackathon_router.patch(
    path="/{id}", response={200: HackathonSchema, ERROR_CODES: ErrorSchema}
)
async def edit_hackathons(
    request: APIRequest, id: uuid.UUID, edit_schema: HackathonEditSchema
):
    user = request.user
    hackathon = await aget_object_or_404(
        Hackathon.objects.select_related("creator"), id=id
    )
    if hackathon.creator != user:
        return 403, ErrorSchema(
            detail="You are not creator and you can not edit this hackathon"
        )

    if edit_schema.name:
        hackathon.name = edit_schema.name
    if edit_schema.description:
        hackathon.description = edit_schema.description
    if edit_schema.min_participants:
        hackathon.min_participants = edit_schema.min_participants
    if edit_schema.max_participants:
        hackathon.max_participants = edit_schema.max_participants
    await hackathon.asave()
    return 200, await hackathon.to_entity()


@hackathon_router.post(
    path="/{id}/change_photo",
    response={200: HackathonSchema, ERROR_CODES: ErrorSchema},
)
async def change_photo(
    request: APIRequest, id: uuid.UUID, image_cover: UploadedFile = File(...)
):
    user = request.user
    hackathon = await aget_object_or_404(
        Hackathon.objects.select_related("creator"), id=id
    )
    if hackathon.creator != user:
        return 403, ErrorSchema(
            detail="You are not creator and you can not edit this hackathon"
        )

    hackathon.image_cover = image_cover.read()
    await hackathon.asave()

    return 200, await hackathon.to_entity()


@hackathon_router.get(
    path="/{id}",
    response={200: HackathonSchema, ERROR_CODES: ErrorSchema},
)
async def get_specific_hackathon(
    request: APIRequest, id: uuid.UUID
) -> tuple[int, Hackathon]:
    hackathon = await aget_object_or_404(
        Hackathon.objects.select_related("creator"), id=id
    )
    return 200, await hackathon.to_entity()


@my_hackathon_router.get(
    path="/", response={200: list[HackathonSchema], ERROR_CODES: ErrorSchema}
)
async def list_my_hackathons(request: APIRequest):
    user = request.user
    hackathons_queryset = Hackathon.objects.filter(
        Q(creator=user) | Q(participants=user)
    ).select_related("creator")
    hackathons = [
        await hackathon.to_entity() async for hackathon in hackathons_queryset
    ]

    return 200, hackathons


@hackathon_router.get(
    path="/get_user_team/{id}",
    response={200: TeamSchema, ERROR_CODES: ErrorSchema},
)
async def get_user_team_in_hackathon(request: APIRequest, id: uuid.UUID):
    user = request.user
    team = await aget_object_or_404(Team, hackathon_id=id, team_members=user)

    return 200, await team.to_entity()


@hackathon_router.post(
    path="/{hackathon_id}/upload_emails",
    response={200: StatusSchema, ERROR_CODES: ErrorSchema},
)
async def upload_emails_to_hackathon(
    request: APIRequest, hackathon_id: uuid.UUID, csv_file: UploadedFile = File(...)
):
    user = request.user
    hackathon = await aget_object_or_404(
        Hackathon.objects.select_related("creator"), id=hackathon_id
    )

    # Проверка, является ли пользователь создателем хакатона
    if hackathon.creator != user:
        return 403, ErrorSchema(
            detail="You are not the creator and can not edit this hackathon"
        )

    try:
        emails = get_emails_from_csv(file=csv_file)
        for email in emails:
            # Создание или получение объекта Email
            email_obj, created = await Email.objects.aget_or_create(email=email)

            # Добавление email в хакатон
            await hackathon.emails.aadd(email_obj)

        await hackathon.asave()

    except Exception as e:
        logger.critical(f"Failed to process CSV file: {str(e)}")
        return 400, ErrorSchema(detail="Failed to process CSV file")

    return 200, StatusSchema()


@hackathon_router.get(
    path="/{hackathon_id}/export",
    response={200: str, ERROR_CODES: ErrorSchema},
)
async def export_participants_hackathon(request: APIRequest, hackathon_id: uuid.UUID):
    user = request.user
    hackathon = await aget_object_or_404(Hackathon.objects, id=hackathon_id)

    if hackathon.creator_id != user.id:
        return 403, ErrorSchema(
            detail="You do not have permission to access this hackathon"
        )

    csv_output = await make_csv(hackathon)

    response = HttpResponse(csv_output, content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="hackathon_{hackathon_id}_participants.csv"'
    )
    return response


@hackathon_router.post(
    path="/{hackathon_id}/start",
    response={200: StatusSchema, ERROR_CODES: ErrorSchema},
)
async def start_hackathon(request: APIRequest, hackathon_id: uuid.UUID):
    hackathon = await aget_object_or_404(
        Hackathon.objects.select_related("creator"), id=hackathon_id
    )
    if hackathon.creator != request.user:
        return 403, ErrorSchema(
            detail="You are not the creator or cannot edit this hackathon"
        )

    hackathon.status = Hackathon.Status.STARTED
    await hackathon.asave()

    await send_notification(
        emails=hackathon.emails.all(),
        context={"hackathon": hackathon},
        mail_template="hackathons/mail/invitation_to_hackathon.html",
        telegram_template="hackathons/telegram/invitation_to_hackathon.html",
    )

    return 200, StatusSchema()


@hackathon_router.post(
    path="/{hackathon_id}/end",
    response={200: StatusSchema, ERROR_CODES: ErrorSchema},
)
async def end_hackathon(request: APIRequest, hackathon_id: uuid.UUID):
    hackathon = await aget_object_or_404(
        Hackathon.objects.select_related("creator"), id=hackathon_id
    )
    if hackathon.creator != request.user:
        return 403, ErrorSchema(
            detail="You are not the creator or cannot edit this hackathon"
        )

    hackathon.status = Hackathon.Status.ENDED
    await hackathon.asave()

    await send_notification(
        emails=hackathon.emails.all(),
        context={"hackathon": hackathon},
        mail_template="hackathons/mail/hackathon_ended.html",
        telegram_template="hackathons/telegram/hackathon_ended.html",
    )

    return 200, StatusSchema()


@hackathon_router.get(
    path="{hackathon_id}/analytic",
    response={200: AnalyticsSchema, ERROR_CODES: ErrorSchema},
)
async def analytics(
    request: APIRequest, hackathon_id: uuid.UUID
) -> tuple[int, AnalyticsSchema | ErrorSchema]:
    hackathon = await aget_object_or_404(Hackathon, id=hackathon_id)
    if hackathon.creator_id != request.user.id:
        return 403, ErrorSchema(detail="You are not the creator")

    hackathon_participants_count = await hackathon.participants.acount()
    users_with_team_count = await Team.team_members.through.objects.filter(
        team__hackathon=hackathon
    ).acount()

    if not hackathon_participants_count:
        return 200, AnalyticsSchema(procent=100)

    procent = (users_with_team_count / hackathon_participants_count) * 100

    return 200, AnalyticsSchema(procent=procent)


@hackathon_router.get(
    path="/{hackathon_id}/summary",
    response={200: HackathonSummarySchema, ERROR_CODES: ErrorSchema},
)
async def hackathon_summary(request: APIRequest, hackathon_id: uuid.UUID):
    hackathon = await aget_object_or_404(Hackathon, id=hackathon_id)
    if hackathon.creator_id != request.user.id:
        return 403, ErrorSchema(detail="You are not the creator")

    # Общее количество команд
    total_teams = await Team.objects.filter(hackathon=hackathon).acount()

    # Количество команд с максимальным количеством участников (полные команды)
    full_teams = await (
        Team.objects.filter(hackathon=hackathon)
        .annotate(num_members=Count("team_members"))
        .filter(num_members=hackathon.max_participants)
        .acount()
    )

    # Процент полных команд
    percent_full_teams = (full_teams / total_teams) * 100 if total_teams > 0 else 0

    # Список людей без команд
    people_without_teams = hackathon.participants.exclude(
        team_members__hackathon=hackathon
    )

    # Количество людей в командах из тех, кто зарегистрировался
    people_in_teams = await (
        Team.objects.filter(hackathon=hackathon)
        .values_list("team_members", flat=True)
        .distinct()
        .acount()
    )

    # Количество приглашенных людей (по количеству emails в хакатоне)
    invited_people = await hackathon.emails.acount()

    # Количество людей, принявших приглашение (по количеству участников)
    accepted_invite = await hackathon.participants.acount()

    return HackathonSummarySchema(
        total_teams=total_teams,
        full_teams=full_teams,
        percent_full_teams=percent_full_teams,
        people_without_teams=[
            await user.to_entity() async for user in people_without_teams
        ],
        people_in_teams=people_in_teams,
        invited_people=invited_people,
        accepted_invite=accepted_invite,
    )


@hackathon_router.get(
    path="/{hackathon_id}/participants_without_team",
    response={200: list[ProfileSchema], ERROR_CODES: ErrorSchema},
)
async def get_participants_without_team(request: APIRequest, hackathon_id: uuid.UUID):
    # Получаем хакатон по id
    hackathon = await aget_object_or_404(Hackathon, id=hackathon_id)
    if hackathon.creator_id != request.user.id:
        return 403, ErrorSchema(detail="You are not the creator")

    # Вычисляем участников, которые не входят в команды
    participants_without_team = hackathon.participants.exclude(
        team_members__hackathon=hackathon
    )

    # Возвращаем список участников без команды с именем, ролью и id
    return 200, [
        await participant.to_entity() async for participant in participants_without_team
    ]


@hackathon_router.get(
    path="/{hackathon_id}/pending_invitations",
    response={200: list[NotificationStatusSchema], ERROR_CODES: ErrorSchema},
)
async def pending_invitations(request: APIRequest, hackathon_id: uuid.UUID):
    # Получаем хакатон по id
    hackathon = await aget_object_or_404(Hackathon, id=hackathon_id)
    if hackathon.creator_id != request.user.id:
        return 403, ErrorSchema(detail="You are not the creator")

    pending_emails = hackathon.emails.exclude(
        email__in=hackathon.participants.values_list("email", flat=True)
    )

    result = []
    async for pending_email in pending_emails:
        notification_status = await NotificationStatus.objects.filter(
            email=pending_email.email
        ).afirst()

        if notification_status is not None:
            send_tg_status = notification_status.telegram_sent
            send_email_status = notification_status.email_sent
        else:
            send_tg_status = True
            send_email_status = True

        result.append(
            NotificationStatusSchema(
                email=pending_email.email,
                send_tg_status=send_tg_status,
                send_email_status=send_email_status,
            )
        )

    return 200, result
