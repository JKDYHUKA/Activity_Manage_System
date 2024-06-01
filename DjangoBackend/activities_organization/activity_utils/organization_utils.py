from datetime import datetime, timedelta, timezone
import numpy as np
from scipy.optimize import linprog
from ..models import CreateActivity, TimeOption, ActivityTime, Notice
from django.conf import settings
from collections import defaultdict
import jwt


def get_number(activity_level):
    if activity_level == '0-50':
        return 1
    elif activity_level == '50-100':
        return 2
    elif activity_level == '100-200':
        return 3
    else:
        return -1


def decode_jwt_token(header):
    authorization = header.get('Authorization')
    jwt_token = authorization.split(' ')[1]

    decoded_token = jwt.decode(jwt_token, settings.SECRET_KEY, algorithms=['HS256'])
    return decoded_token


def calculate_hours_difference_from_tomorrow_midnight(date_str):
    # 解析日期时间字符串为 datetime 对象
    date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))

    # 获取当前时间
    now = datetime.utcnow() + timedelta(hours=8)
    # 计算明天凌晨0点的时间
    tomorrow_midnight = datetime(now.year, now.month, now.day) + timedelta(days=1)
    tomorrow_midnight = tomorrow_midnight.replace(tzinfo=None)
    date = date.replace(tzinfo=None)

    # 计算时间差（以秒为单位）
    diff_in_seconds = (date - tomorrow_midnight).total_seconds()
    # 将时间差转换为小时
    diff_in_hours = diff_in_seconds / 3600
    return diff_in_hours + 8


def list_to_tuple_with_processing(time_list):
    assert len(time_list) == 2, "the length of time_list only accept 2"

    start_time = int(time_list[0])
    end_time = int(time_list[1] + 1)
    return start_time, end_time


def activities_manage():
    # 有效时间区域（8点到20点）
    time_slots = [(i, i + 1) for i in range(6, 23)]

    # 场地数量
    venue_count = {
        '小': 1,
        '中': 2,
        '大': 1
    }

    # 设定活动类型和优先级
    activity_priority = {
        '会议': 1,
        '研讨会': 2,
        '培训': 3,
        '社交活动': 4
    }

    # 生成算法的输入
    activity_requests = construct_activity_requests()

    # 按照活动类型优先级排序
    activity_requests.sort(key=lambda x: activity_priority[x['type']], reverse=True)

    # 按规模分类活动请求
    activities_by_scale = {'小': [], '中': [], '大': []}
    for activity in activity_requests:
        activities_by_scale[activity['scale']].append(activity)

    # 分别处理每个规模的活动
    successful_results = {}
    unsuccessful_results = {}
    for scale in ['小', '中', '大']:
        if not activities_by_scale[scale]:
            continue
        successful_results[scale], unsuccessful_results[scale] = optimize_activities(scale, activities_by_scale[scale],
                                                                                     venue_count, time_slots)
    print(successful_results)
    activity_ids = []
    result_start_hours = []
    result_end_hours = []
    # result example: ('e9733794-9fe8-4b97-a345-23d5989c719d', (17, 19))
    for scale in successful_results:
        for result in successful_results[scale]:
            activity_ids.append(result[0])
            result_start_hours.append(result[1][0])
            result_end_hours.append(result[1][1])

    # filter得到的结果不是按顺序的，所以要自己排序
    activities = CreateActivity.objects.filter(activity_id__in=activity_ids)
    activities_dict = {str(act.activity_id): act for act in activities}
    # 此时sorted_act数组中，活动对象的顺序与activity_ids数组中活动对象的顺序一样
    sorted_act = [activities_dict[act_id] for act_id in activity_ids]

    # 先将这些活动的所有time_option都利用filter选择出来
    time_options = TimeOption.objects.filter(activity__activity_id__in=activity_ids)
    filtered_time_options = []

    # 在该循环里筛选符合条件的TimeOption, 并返回[[], [], []] shape=(n, m), m<=3  考虑到会有三个志愿时间一样的情况，此时选择列表中每一行
    # 的第一个当作最终时间即可
    for activity, start_time_hours, end_time_hours in zip(sorted_act, result_start_hours, result_end_hours):
        filtered_time_options.append(
            [time_option for time_option in time_options if time_option.activity==activity and time_option.start_time_hours==start_time_hours and time_option.end_time_hours==end_time_hours]
        )

    activity_times_to_create = [
        ActivityTime(
            activity=act,
            start_time=time_option[0].start_time,
            start_time_hours=time_option[0].start_time_hours,
            end_time=time_option[0].end_time,
            end_time_hours=time_option[0].end_time_hours
        )
        for act, time_option in zip(sorted_act, filtered_time_options)
    ]

    ActivityTime.objects.bulk_create(activity_times_to_create)

    # 输出结果
    for scale in successful_results:
        print("**************************************************************************")
        print(f"Successful Results for {scale} venues:")
        for res in successful_results[scale]:
            activity_id, time_slot = res
            print(f"Activity ID {activity_id} assigned to {scale} venue at time slot {time_slot}")
        print(f"Unsuccessful Results for {scale} venues:")
        for res in unsuccessful_results[scale]:
            activity_id, time_slot = res
            print(f"Activity ID {activity_id} unassigned from {scale} venue at time slot {time_slot}")

    activities.update(activity_condition=True)

    # 将所有的待发送的通知取出来
    notices = Notice.objects.filter(condition=False)
    # 按照活动将每个通知分类, 通知字典的结构如下
    # {
    #  ’活动id1‘: [通知1， 通知2， 通知3...],
    #  '活动id2‘: [通知n， 通知n+1， 通知n+2...],
    #        ......
    # }
    notices_dict = {str(key.activity_id): [] for key in sorted_act}
    for notice in notices:
        notices_dict[notice.activity_id].append(notice)

    # 遍历排序好的活动数组，顺序很重要！
    # 按sorted_act中的顺序，来构建通知数组，其形状如下
    # [
    #  [活动1的所有通知], [活动2的所有通知], ...
    # ]
    # 其对应着时间数组filtered_time_options
    # [
    #  [活动1的最终时间], [活动2的最终时间], ...
    # ]
    sorted_notices = []
    for act in sorted_act:
        sorted_notices.append(notices_dict[str(act.activity_id)])

    notices_to_be_updated = []
    for act, sub_notices, filtered_time_option in zip(sorted_act, sorted_notices, filtered_time_options):
        activity_name = act.activity_name

        activity_start_time = filtered_time_option[0].start_time
        format_start_time = convert_to_beijing_time(activity_start_time)

        activity_end_time = filtered_time_option[0].end_time
        format_end_time = convert_to_beijing_time(activity_end_time)
        for notice in sub_notices:
            role = notice.content
            notice.content = generate_invitation_content(activity_name, format_start_time, format_end_time, role)
            notice.condition = True

        notices_to_be_updated.extend(sub_notices)

    Notice.objects.bulk_update(notices_to_be_updated, ['content', 'condition'])

    return True


def construct_single_activity_request(activity):
    if activity.activity_type == 'meeting':
        activity_type = '会议'
    else:
        raise ValueError("活动类型错误：期望为 'meeting'，但得到了 {}".format(activity.type))

    if activity.activity_level == 1:
        scale = '小'
    elif activity.activity_level == 2:
        scale = '中'
    elif activity.activity_level == 3:
        scale = '大'
    else:
        raise ValueError("活动规模错误：期望为 '大','中','小', 但得到了 {}".format(activity.type))

    activity_id = activity.activity_id
    time_options = TimeOption.objects.filter(activity__activity_id=activity_id)
    preferred_time_slots = []
    for option in time_options:
        time_tuple = option.start_time_hours, option.end_time_hours
        preferred_time_slots.append(time_tuple)

    duration = preferred_time_slots[0][1] - preferred_time_slots[0][0]

    single_activity_request = {'id': str(activity.activity_id),
                               'type': activity_type,
                               'scale': scale,
                               'duration': duration,
                               'preferred_time_slots': preferred_time_slots}
    return single_activity_request


def construct_activity_requests():
    activity_request = []
    activities = CreateActivity.objects.filter(activity_condition=False)
    for activity in activities:
        activity_request.append(construct_single_activity_request(activity))

    return activity_request


def optimize_activities(scale, activities, venue_count, time_slots):
    num_classes = venue_count[scale]  # 资源数目
    num_activities = len(activities)
    num_time_slots = len(time_slots)
    num_vars = num_activities * 3  # 每个活动有三个时间段选择

    # 目标函数：最大化分配的活动数
    c = np.ones(num_vars) * -1  # 转换为最小化问题

    # 约束条件
    A = []
    b = []

    # 时间段使用情况约束
    slot_usage = np.zeros((num_time_slots, num_vars))
    activity_indices = {i: [] for i in range(num_activities)}  # 记录每个活动对应的变量索引

    # 填充时间段使用和活动对应变量索引
    var_index = 0
    for i, activity in enumerate(activities):
        for time_slot in activity['preferred_time_slots']:
            start, end = time_slot
            for hour in range(start, end):
                slot_usage[hour - 8, var_index] = 1
            activity_indices[i].append(var_index)
            var_index += 1

    # 添加时间段使用约束
    for hour in range(num_time_slots):
        A.append(slot_usage[hour, :])
        b.append(num_classes)  # 每个时间段最多只能有一个活动

    # 每个活动只能选择一个时间段
    for indices in activity_indices.values():
        activity_constraint = np.zeros(num_vars)
        for idx in indices:
            activity_constraint[idx] = 1
        A.append(activity_constraint)
        b.append(1)

    A = np.array(A)
    b = np.array(b)

    # 解线性规划问题
    res = linprog(c, A_ub=A, b_ub=b, bounds=(0, 1), method='highs')

    # 解析结果
    assigned_activities = []
    unassigned_activities = []
    if res.success:
        for i in range(num_activities):
            allocated = False
            for var_idx in activity_indices[i]:
                if res.x[var_idx] > 0.5:
                    assigned_activities.append((activities[i]['id'], activities[i]['preferred_time_slots'][var_idx % 3]))
                    allocated = True
                    break
            if allocated == False:
                unassigned_activities.append((activities[i]['id'], activities[i]['preferred_time_slots']))
    return assigned_activities, unassigned_activities


def convert_to_beijing_time(timestamp_str):
    # 将字符串转换为 datetime 对象
    dt = datetime.fromisoformat(timestamp_str[:-1])  # 去除末尾的 'Z' 字符并转换为 datetime 对象

    # 将时间转换为北京时间（东八区）
    beijing_time = dt.astimezone(timezone(timedelta(hours=8)))

    # 格式化时间
    formatted_time = beijing_time.strftime("%Y年%m月%d日%H时")

    return formatted_time


def generate_invitation_content(act_name, start_time, end_time, role):
    return f"尊敬的用户您好，您已经被邀请参加\"{act_name}\"活动，该活动的预计持续时间为：北京时间{start_time}到{end_time}，您在该活动中的角色为{role}"


def generate_activity_details(activities):
    activity_times = ActivityTime.objects.filter(activity__in=activities)

    # 其结构如下
    # {'e9733794-9fe8-4b97-a345-23d5989c719d': ['2024-06-02T09:52:58.000Z', '2024-06-02T10:52:58.000Z']}
    activity_time_dict = defaultdict(list)
    for activity_time in activity_times:
        activity_time_dict[str(activity_time.activity_id)].extend([activity_time.start_time, activity_time.end_time])

    activity_details = []
    for act in activities:
        start_time = convert_to_beijing_time(activity_time_dict[str(act.activity_id)][0])
        end_time = convert_to_beijing_time(activity_time_dict[str(act.activity_id)][1])
        details = {
            "act_name": act.activity_name,
            "act_describe": act.activity_description,
            "act_create_user": act.activity_leader.username,
            "act_time": f"北京时间{start_time}到{end_time}"
        }
        activity_details.append(details)

    return activity_details