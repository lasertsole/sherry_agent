/**
 * 比较string或number大小，选出大的
 * 
 * @param { T extends string | number } a
 * @param { T extends string | number } b 
 * @returns { T } a和b中较大的值
 */
export function max<T extends number | string>(a: T, b: T): T {
    if (a > b) {
        return a;
    } else {
        return b;
    }
}

/**
 * 比较string或number大小，选出小的
 * 
 * @param { T extends string | number } a
 * @param { T extends string | number } b 
 * @returns { T } a和b中较小的值
 */
export function min<T extends number | string>(a: T, b: T): T {
    if (a < b) {
        return a;
    } else {
        return b;
    }
}

/**
 * 将日期转换为格式化字符串(本地化时间)
 * 
 * @param { Date } date
 * @returns { string } 格式化后的日期字符串
 */
export function getFormattedDate(date: Date):string {
    const now = new Date();
    const diff = now.getTime() - date.getTime();
    const oneDay = 24 * 60 * 60 * 1000; // 一天的毫秒数
    const oneYear = 365 * oneDay; // 一年的毫秒数

    const options: Intl.DateTimeFormatOptions = {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: 'numeric',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
    };

    const formattedDate = date.toLocaleString('en-US', options);
    const [timePart, amPm] = formattedDate.split(', ');
    const [monthDayYear] = timePart.split(' ');
    const [month, day, year] = monthDayYear.split('/');

    if (diff >= oneYear) {
        return `${year}.${month}.${day}`;
    } else if (diff >= oneDay) {
        return `${month}.${day}`;
    } else {
        return `${amPm}`;
    };
};

/**
 * 将字符串转换为日期
 * 
 * @param { string } dateString
 * @returns { Date | null } 日期
 */
export function stringToDate(dateString: string): Date | null {
    // 检查是否为数字，可能是时间戳
    if (!isNaN(Number(dateString))) {
        const timestamp = Number(dateString);
        const date = new Date(timestamp);

        if (isNaN(date.getTime())) {
        console.error('Invalid date:', dateString);
            return null; // 或者返回其他默认值
        };

        return date;
    }

    // 将自定义格式转换为标准 ISO 8601 格式
    dateString=dateString.replace(/T/gi, ' ').replace(/Z/gi, "");
    const isoTime = dateString.replace(' ', 'T') + 'Z';
    const date = new Date(dateString);
    if (isNaN(date.getTime())) {
        console.error('Invalid date:', dateString);
        return null; // 或者返回其他默认值
    };

    return date;
};

/**
 * 计算属性，用于将 UTC 时间转换为本地时间
 * 
 * @param { string | undefined } utcTime
 * @returns { string | null } 本地时间字符串
 */
export function formatToLocalTime(utcTime: string | undefined):string | null {
    if (!utcTime) return '';

    let date:Date | null = stringToDate(utcTime);
    if (!date) {
        return null
    };

    return getFormattedDate(date);
};

/**
 * 比较Date时间
 * 
 * @param { Date } a 
 * @param { Date } b 
 * @returns a与b的时间差,若为正数，则a在b之后，若为负数，则a在b之前，若为0，则a和b相等
 */
export function compareDate(a: Date, b: Date): number {
    return a.getTime() - b.getTime();
};

/**
 * 判断Date时间是否晚于另一个Date时间
 * 
 * @param { Date } a 
 * @param { Date } b 
 * @returns { boolean } 若a晚于b，则返回true，否则返回false
 */
export function isLate(a: Date, b: Date):boolean {
    return a.getTime() > b.getTime();
};

/**
 * 筛选出最大的Date
 * 
 * @param { Date } a 
 * @param { Date } b 
 * @returns { Date } 若a晚于b，则返回true，否则返回false
 */
export function maxDate(a: Date, b: Date): Date {
    return isLate(a, b) ? a : b;
};

/**
 * 当前的UTC国际通用时间,精确到微秒级别
 * 
 * @returns { string } 当前的UTC国际通用时间
 */
export function getUTCTimeNow(): string {
    const now = new Date();
    const year = now.getUTCFullYear();
    const month = now.getUTCMonth() + 1;
    const day = now.getUTCDate();
    const hours = now.getUTCHours();
    const minutes = now.getUTCMinutes();
    const seconds = now.getUTCSeconds();
    const milliseconds = now.getUTCMilliseconds();
    const microseconds = (milliseconds * 1000 + Math.floor((now.getTime() % 1) * 1000000)) % 1000000;
    return year + '-' + month + '-' + day + 'T' + hours + ':' + minutes + ':' + seconds + '.' + microseconds.toString().padStart(6, '0') + 'Z';
};

/**
 * 判断string是否是时间戳
 * 
 * @param str 需要判断的字符串
 * @returns 是否是时间戳,是true,不是false
 */
export function isTimestamp(str: string): boolean {
    try {
        const date = new Date(str);
        if (!isNaN(date.getTime())) {
            return true;
        };
        return false;
    } catch (error) {
        return false;
    };
};