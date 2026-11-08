"""Все таблицы. Импорт отсюда регистрирует их в Base.metadata."""
from .org import Branch, Setting, Lift, OrderCounter  # noqa: F401
from .staff import (  # noqa: F401
    Role, Permission, RolePermission, User, UserPermission,
    StaffCard, AccessPoint, AttendanceEvent,
)
from .payroll import (  # noqa: F401
    PayScheme, PaySchemeLine, UserPayScheme, PartGroup, PartGroupItem,
    PayrollAccrual, PayrollPayout,
)
from .orders import (  # noqa: F401
    Client, Vehicle, WorkOrder, WoStatusHistory, WoPhoto, WoFinding,
    WoWork, WoPart, WoWorkSession, ClientEvent,
)
from .stock import (  # noqa: F401
    PartCatalog, Location, StockItem, StockPhoto, StockMovement,
    Supplier, Purchase, PurchaseLine, IssueRequest, IssueRequestLine,
)
from .money import Payment, AuditLog  # noqa: F401
from .tools import Tool, ToolMove, ToolCheck, ToolCheckLine  # noqa: F401
