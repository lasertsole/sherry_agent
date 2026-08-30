/**
 * Compare two string or number values and pick the larger one
 *
 * @param { T extends string | number } a
 * @param { T extends string | number } b
 * @returns { T } the larger of a and b
 */
export function max<T extends number | string>(a: T, b: T): T {
  if (a > b) {
    return a;
  } else {
    return b;
  }
}

/**
 * Compare two string or number values and pick the smaller one
 *
 * @param { T extends string | number } a
 * @param { T extends string | number } b
 * @returns { T } the smaller of a and b
 */
export function min<T extends number | string>(a: T, b: T): T {
  if (a < b) {
    return a;
  } else {
    return b;
  }
}

/**
 * Convert a date into a formatted string (localized time)
 *
 * @param { Date } date
 * @returns { string } the formatted date string
 */
export function getFormattedDate(date: Date): string {
  const now = new Date();
  const diff = now.getTime() - date.getTime();
  const oneDay = 24 * 60 * 60 * 1000; // milliseconds in one day
  const oneYear = 365 * oneDay; // milliseconds in one year

  const options: Intl.DateTimeFormatOptions = {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  };

  const formattedDate = date.toLocaleString('en-US', options);
  // Empty-string defaults keep the output identical for well-formed input (split() always yields
  // the parts here); they only satisfy noUncheckedIndexedAccess for malformed strings.
  const [timePart = '', amPm] = formattedDate.split(', ');
  const [monthDayYear = ''] = timePart.split(' ');
  const [month, day, year] = monthDayYear.split('/');

  if (diff >= oneYear) {
    return `${year}.${month}.${day}`;
  } else if (diff >= oneDay) {
    return `${month}.${day}`;
  } else {
    return `${amPm}`;
  }
}

/**
 * Convert a string into a date
 *
 * @param { string } dateString
 * @returns { Date | null } the date
 */
export function stringToDate(dateString: string): Date | null {
  // Check whether it is a number, which may be a timestamp
  if (!isNaN(Number(dateString))) {
    const timestamp = Number(dateString);
    const date = new Date(timestamp);

    if (isNaN(date.getTime())) {
      console.error('Invalid date:', dateString);
      return null; // or return some other default value
    }

    return date;
  }

  // Convert the custom format into a standard ISO 8601 format
  dateString = dateString.replace(/T/gi, ' ').replace(/Z/gi, '');
  const date = new Date(dateString);
  if (isNaN(date.getTime())) {
    console.error('Invalid date:', dateString);
    return null; // or return some other default value
  }

  return date;
}

/**
 * Computed-style helper for converting UTC time into local time
 *
 * @param { string | undefined } utcTime
 * @returns { string | null } the local time string
 */
export function formatToLocalTime(utcTime: string | undefined): string | null {
  if (!utcTime) return '';

  const date: Date | null = stringToDate(utcTime);
  if (!date) {
    return null;
  }

  return getFormattedDate(date);
}

/**
 * Compare two Date values
 *
 * @param { Date } a
 * @param { Date } b
 * @returns the time difference between a and b; positive means a is after b,
 *   negative means a is before b, zero means a and b are equal
 */
export function compareDate(a: Date, b: Date): number {
  return a.getTime() - b.getTime();
}

/**
 * Check whether one Date is later than another Date
 *
 * @param { Date } a
 * @param { Date } b
 * @returns { boolean } true if a is later than b, false otherwise
 */
export function isLate(a: Date, b: Date): boolean {
  return a.getTime() > b.getTime();
}

/**
 * Pick the later of two Dates
 *
 * @param { Date } a
 * @param { Date } b
 * @returns { Date } true if a is later than b, false otherwise
 */
export function maxDate(a: Date, b: Date): Date {
  return isLate(a, b) ? a : b;
}

/**
 * The current UTC (universal coordinated) time, precise to the microsecond level
 *
 * @returns { string } the current UTC time
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
  return (
    year +
    '-' +
    month +
    '-' +
    day +
    'T' +
    hours +
    ':' +
    minutes +
    ':' +
    seconds +
    '.' +
    microseconds.toString().padStart(6, '0') +
    'Z'
  );
}

/**
 * Check whether a string is a timestamp
 *
 * @param str the string to check
 * @returns whether it is a timestamp: true if yes, false if not
 */
export function isTimestamp(str: string): boolean {
  try {
    const date = new Date(str);
    if (!isNaN(date.getTime())) {
      return true;
    }
    return false;
  } catch {
    return false;
  }
}
