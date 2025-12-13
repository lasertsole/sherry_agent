export class StaffConfig {// 单例模式
    private static instance: StaffConfig = new StaffConfig();
    private constructor() {}
    public static getInstance(): StaffConfig {
        return this.instance;
    }

    private paddingY: Ref<number> = ref<number>(5);
    public getPaddingY(): Ref<number> {
        return this.paddingY;
    }

    public setPaddingY(paddingY: number): void {
        if(paddingY<=0 || paddingY >= 100) {
            return;
        }
        this.paddingY.value = paddingY;
    }


    private staffNum: Ref<number> = ref<number>(5);
    public getStaffNum(): Ref<number> {
        return this.staffNum;
    }
    public setStaffNum(staffNum: number): void {
        if(staffNum<=0 || staffNum >= 50) {
            return;
        }
        this.staffNum.value = staffNum;
    }


    private heightPercent: Ref<number> = ref<number>(10);
    public getHeightPercent(): Ref<number> {
        return this.heightPercent;
    }
    public setHeightPercent(heightPercent: number): void {
        if(heightPercent<=10 || heightPercent >= 100) {
            return;
        }
        this.heightPercent.value = heightPercent;
    }


    private gapPerStaff: Ref<number> = ref<number>(5);
    public getGapPerStaff(): Ref<number> {
        return this.gapPerStaff;
    }
    public setGapPerStaff(gapPerStaff: number): void {
        if(gapPerStaff<=0 || gapPerStaff >= 100) {
            return;
        }
        this.gapPerStaff.value = gapPerStaff;
    }


    private biasPerStaff: ComputedRef<number> = computed(()=>{
        return this.heightPercent.value + this.gapPerStaff.value;
    });

    public getBaisPerStaff(): ComputedRef<number> {
        return this.biasPerStaff;
    }


    private maxStaffNumPerPage: ComputedRef<number> = computed(()=>{
        return Math.floor((100 - 2 * this.paddingY.value) / this.biasPerStaff.value);
    });
    public getMaxStaffNumPerPage(): ComputedRef<number> {
        return this.maxStaffNumPerPage;
    }


    private pageNum: ComputedRef<number> = computed(()=>{
        return Math.ceil(this.staffNum.value / this.maxStaffNumPerPage.value);
    });

    public getPageNum(): ComputedRef<number> {
        return this.pageNum;
    }


    private pageIndex: Ref<number> = ref<number>(1);
    public getPageIndex(): Ref<number> {
        return this.pageIndex;
    }
    public setPageIndex(pageIndex: number): void {
        if(pageIndex<=0 || pageIndex > this.pageNum.value) {
            return;
        }
        this.pageIndex.value = pageIndex;
    }


    private staffNumOfCurrentPage: ComputedRef<number> = computed(()=>{
        if(this.pageIndex.value === this.pageNum.value) {
            return this.staffNum.value - (this.pageNum.value - 1) * this.maxStaffNumPerPage.value;
        } else {
            return this.maxStaffNumPerPage.value;
        }
    });

    public getStaffNumOfcurrentPage(): ComputedRef<number> {
        return this.staffNumOfCurrentPage;
    }
}