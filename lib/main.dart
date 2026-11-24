import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:webview_flutter/webview_flutter.dart';
import 'package:webview_flutter_android/webview_flutter_android.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:connectivity_plus/connectivity_plus.dart';

// =========================================================================
// 🌐 CONFIGURATION: رابط خادم المنصة الدائم بشهادة HTTPS
// =========================================================================
class AppConfig {
  static const String baseUrl = "https://barberqueue-dz.onrender.com";
  static const String serverUrl = "https://barberqueue-dz.onrender.com/portal";
}

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // إعداد شريط الحالة وأزرار التنقل السفلية بنمط داكن فخم
  SystemChrome.setSystemUIOverlayStyle(
    const SystemUiOverlayStyle(
      statusBarColor: Colors.transparent,
      statusBarIconBrightness: Brightness.light,
      systemNavigationBarColor: Color(0xFF0F172A),
      systemNavigationBarIconBrightness: Brightness.light,
    ),
  );

  runApp(const BarberQueueNativeApp());
}

class BarberQueueNativeApp extends StatelessWidget {
  const BarberQueueNativeApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'دورك (Dorek)',
      debugShowCheckedModeBanner: false,
      locale: const Locale('ar'),
      supportedLocales: const [Locale('ar'), Locale('fr')],
      localizationsDelegates: const [
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: const Color(0xFF0B0F19),
        primaryColor: const Color(0xFFF59E0B), // Amber 500
        colorScheme: const ColorScheme.dark(
          primary: Color(0xFFF59E0B),
          secondary: Color(0xFF10B981), // Emerald 500
          surface: Color(0xFF0F172A),
        ),
        fontFamily: 'Tajawal',
      ),
      home: const SplashScreen(),
    );
  }
}

// =========================================================================
// 1. NATIVE SPLASH SCREEN (شاشة البداية بدون طلب الكاميرا)
// =========================================================================
class SplashScreen extends StatefulWidget {
  const SplashScreen({super.key});

  @override
  State<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends State<SplashScreen>
    with SingleTickerProviderStateMixin {
  late AnimationController _animController;
  late Animation<double> _fadeAnimation;

  @override
  void initState() {
    super.initState();
    _animController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1200),
    );
    _fadeAnimation = CurvedAnimation(
      parent: _animController,
      curve: Curves.easeInOut,
    );
    _animController.forward();

    _requestPermissionsAndNavigate();
  }

  Future<void> _requestPermissionsAndNavigate() async {
    // طلب صلاحية الموقع فقط (GPS) وقت الإقلاع دون طلب الكاميرا
    try {
      await Permission.locationWhenInUse.request();
    } catch (_) {}

    await Future.delayed(const Duration(milliseconds: 1300));
    if (mounted) {
      Navigator.of(context).pushReplacement(
        PageRouteBuilder(
          transitionDuration: const Duration(milliseconds: 350),
          pageBuilder: (_, __, ___) => const WebViewScreen(),
          transitionsBuilder: (_, animation, __, child) =>
              FadeTransition(opacity: animation, child: child),
        ),
      );
    }
  }

  @override
  void dispose() {
    _animController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF0B0F19),
      body: Center(
        child: FadeTransition(
          opacity: _fadeAnimation,
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Container(
                width: 95,
                height: 95,
                decoration: BoxDecoration(
                  gradient: const LinearGradient(
                    colors: [Color(0xFFF59E0B), Color(0xFFD97706)],
                    begin: Alignment.topLeft,
                    end: Alignment.bottomRight,
                  ),
                  borderRadius: BorderRadius.circular(28),
                  boxShadow: [
                    BoxShadow(
                      color: const Color(0xFFF59E0B).withOpacity(0.35),
                      blurRadius: 24,
                      offset: const Offset(0, 10),
                    ),
                  ],
                ),
                child: const Center(
                  child: Text('💈', style: TextStyle(fontSize: 48)),
                ),
              ),
              const SizedBox(height: 24),
              const Text(
                'دَوْرُك',
                style: TextStyle(
                  fontSize: 34,
                  fontWeight: FontWeight.w900,
                  color: Colors.white,
                  letterSpacing: 1.2,
                ),
              ),
              const SizedBox(height: 6),
              const Text(
                'منظومة الحلاقة الذكية الأولى في الجزائر 🇩🇿',
                style: TextStyle(
                  fontSize: 13,
                  fontWeight: FontWeight.w500,
                  color: Color(0xFF94A3B8),
                ),
              ),
              const SizedBox(height: 36),
              const SizedBox(
                width: 24,
                height: 24,
                child: CircularProgressIndicator(
                  strokeWidth: 2.5,
                  valueColor: AlwaysStoppedAnimation<Color>(Color(0xFFF59E0B)),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// =========================================================================
// 2. MAIN WEBVIEW SCREEN (PRE-WARMED INDEXED STACK - ZERO LATENCY - NO ORANGE BAR)
// =========================================================================
class WebViewScreen extends StatefulWidget {
  const WebViewScreen({super.key});

  @override
  State<WebViewScreen> createState() => _WebViewScreenState();
}

class _WebViewScreenState extends State<WebViewScreen> {
  int _currentIndex = 0;

  // 4 Primary Platform Tab Endpoints
  static const List<String> _tabUrls = [
    '${AppConfig.baseUrl}/portal',
    '${AppConfig.baseUrl}/explore',
    '${AppConfig.baseUrl}/freelance/request',
    '${AppConfig.baseUrl}/login',
  ];

  static const List<String> _tabIcons = [
    '💈',
    '✂️',
    '🛵',
    '🔐',
  ];

  static const List<String> _tabLoadingLabels = [
    'جاري تحميل المنصة الرئيسية...',
    'جاري تحميل دليل الصالونات...',
    'جاري تحميل خدمة حلاق للدار...',
    'جاري فتح فضاء الحساب...',
  ];

  // Pre-warmed Controllers & State per Tab
  final List<WebViewController?> _controllers = [null, null, null, null];
  final List<bool> _isTabLoaded = [false, false, false, false];
  final List<bool> _isPageFinished = [false, false, false, false];
  final List<bool> _hasErrorTab = [false, false, false, false];
  final List<String> _errorMsgTab = ['', '', '', ''];

  @override
  void initState() {
    super.initState();
    // 1. تهيئة التبويب الرئيسي فوراً وبشكل أولي
    _initTab(0);

    // 2. التحميل التدريجي الذكي للتبويبات الأخرى في الخلفية لتفادي مزاحمة شبكة 4G
    _scheduleSequentialPrewarming();

    _checkConnectivity();
  }

  void _scheduleSequentialPrewarming() {
    // تبويب الصالونات بعد 1.2 ثانية
    Future.delayed(const Duration(milliseconds: 1200), () {
      if (mounted && !_isTabLoaded[1]) {
        _initTab(1);
      }
    });

    // تبويب حلاق للدار بعد 2.8 ثانية
    Future.delayed(const Duration(milliseconds: 2800), () {
      if (mounted && !_isTabLoaded[2]) {
        _initTab(2);
      }
    });

    // تبويب الحساب بعد 4.2 ثانية
    Future.delayed(const Duration(milliseconds: 4200), () {
      if (mounted && !_isTabLoaded[3]) {
        _initTab(3);
      }
    });
  }

  void _initTab(int index) {
    if (_isTabLoaded[index]) return;
    _isTabLoaded[index] = true;

    final controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(const Color(0xFF0B0F19))
      ..setUserAgent('BarberQueueNativeApp/1.0.0')
      
      ..setNavigationDelegate(
        NavigationDelegate(
          onPageStarted: (String url) {
            _syncTabHighlightFromUrl(url, index);
          },
          onPageFinished: (String url) {
            if (mounted) {
              setState(() {
                _isPageFinished[index] = true;
                _hasErrorTab[index] = false;
              });
            }
            _syncTabHighlightFromUrl(url, index);

            // إخفاء شريط الويب السفلي داخل التطبيق الأصلي لتفادي التكرار
            _controllers[index]?.runJavaScript("""
              (function() {
                var webNav = document.querySelector('nav.fixed.bottom-0');
                if (webNav) webNav.style.display = 'none';
                var pwaBanner = document.getElementById('pwa-install-banner');
                if (pwaBanner) pwaBanner.style.display = 'none';
                document.body.style.paddingBottom = '0px';
              })();
            """);
          },
          onWebResourceError: (WebResourceError error) {
            if (error.isForMainFrame ?? true) {
              if (mounted) {
                setState(() {
                  _hasErrorTab[index] = true;
                  _errorMsgTab[index] = error.description;
                });
              }
            }
          },
          onNavigationRequest: (NavigationRequest request) {
            final uri = Uri.parse(request.url);

            // 1. فتح روابط الاتصال الهاتفي مباشرة في تطبيق الهاتف
            if (uri.scheme == 'tel') {
              launchUrl(uri);
              return NavigationDecision.prevent;
            }

            // 2. فتح محادثات واتساب في تطبيق WhatsApp الأصلي
            if (uri.scheme == 'whatsapp' ||
                uri.host.contains('wa.me') ||
                uri.host.contains('whatsapp.com')) {
              launchUrl(uri, mode: LaunchMode.externalApplication);
              return NavigationDecision.prevent;
            }

            // 3. فتح روابط خرائط Google Maps في تطبيق الخرائط الأصلي
            if (uri.host.contains('maps.google.com') ||
                uri.host.contains('goo.gl') ||
                uri.host.contains('maps.app.goo.gl')) {
              launchUrl(uri, mode: LaunchMode.externalApplication);
              return NavigationDecision.prevent;
            }

            // 4. التوجيه الفوري اللحظي بين التبويبات (0ms Switch بدون أي إعادة تحميل!)
            final path = uri.path.toLowerCase();
            if (path == '/explore' || path == '/explore/') {
              _switchToTab(1);
              return NavigationDecision.prevent;
            } else if (path == '/freelance/request' || path == '/freelance/request/') {
              _switchToTab(2);
              return NavigationDecision.prevent;
            } else if (path == '/portal' || path == '/portal/' || path == '' || path == '/') {
              _switchToTab(0);
              return NavigationDecision.prevent;
            } else if (path == '/login' || path == '/login/') {
              _switchToTab(3);
              return NavigationDecision.prevent;
            }

            return NavigationDecision.navigate;
          },
        ),
      );

    // تفعيل إعدادات أندرويد ومنح صلاحيات الـ GPS لصفحات الويب تلقائياً
    if (controller.platform is AndroidWebViewController) {
      AndroidWebViewController.enableDebugging(false);
      final androidController = controller.platform as AndroidWebViewController;
      androidController.setGeolocationPermissionsPromptCallbacks(
        onShowPrompt: (request) async {
          var status = await Permission.locationWhenInUse.status;
          if (!status.isGranted) {
            status = await Permission.locationWhenInUse.request();
          }
          return GeolocationPermissionsResponse(
            allow: status.isGranted,
            retain: true,
          );
        },
      );
    }

    _controllers[index] = controller;
    controller.loadRequest(Uri.parse(_tabUrls[index]));
  }

  void _syncTabHighlightFromUrl(String url, int fromIndex) {
    final uri = Uri.tryParse(url);
    if (uri == null) return;
    final path = uri.path.toLowerCase();

    int targetIndex = _currentIndex;
    if (path == '/portal' || path == '' || path == '/') {
      targetIndex = 0;
    } else if (path.startsWith('/explore')) {
      targetIndex = 1;
    } else if (path.startsWith('/freelance/request') || path.startsWith('/freelance/track')) {
      targetIndex = 2;
    } else if (path.startsWith('/login') || path.startsWith('/barber')) {
      targetIndex = 3;
    }

    if (targetIndex != _currentIndex && mounted) {
      setState(() {
        _currentIndex = targetIndex;
      });
    }
  }

  void _switchToTab(int targetIndex) {
    if (!_isTabLoaded[targetIndex]) {
      _initTab(targetIndex);
    }
    if (_currentIndex != targetIndex && mounted) {
      setState(() {
        _currentIndex = targetIndex;
      });
    }
  }

  void _onTabTapped(int index) {
    if (_currentIndex == index) {
      // إعادة النقر على نفس التبويب تعيد التحديث يدوياً
      _reloadTab(index);
      return;
    }
    _switchToTab(index);
  }

  void _reloadTab(int index) {
    if (_controllers[index] != null) {
      setState(() {
        _hasErrorTab[index] = false;
        _isPageFinished[index] = false;
      });
      _controllers[index]!.reload();
    } else {
      _initTab(index);
    }
  }

  Future<void> _checkConnectivity() async {
    final connectivityResult = await Connectivity().checkConnectivity();
    if (connectivityResult.contains(ConnectivityResult.none)) {
      if (mounted) {
        setState(() {
          _hasErrorTab[_currentIndex] = true;
        });
      }
    }
  }

  Widget _buildErrorView(int index) {
    final errorMsg = _errorMsgTab[index];
    return Center(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 28),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Container(
              width: 80,
              height: 80,
              decoration: BoxDecoration(
                color: const Color(0xFF1E293B),
                borderRadius: BorderRadius.circular(24),
                border: Border.all(
                  color: const Color(0xFFF59E0B).withOpacity(0.3),
                ),
              ),
              child: const Center(
                child: Text('📡', style: TextStyle(fontSize: 38)),
              ),
            ),
            const SizedBox(height: 20),
            const Text(
              'تعذر الاتصال بالخادم',
              style: TextStyle(
                fontSize: 20,
                fontWeight: FontWeight.w900,
                color: Colors.white,
              ),
            ),
            const SizedBox(height: 8),
            Text(
              errorMsg.isNotEmpty
                  ? 'السبب: $errorMsg\nيرجى التأكد من تشغيل بيانات 4G أو الواي فاي.'
                  : 'يرجى التأكد من تشغيل بيانات الهاتف (4G) أو الاتصال بشبكة Wi-Fi، ثم أعد المحاولة.',
              textAlign: TextAlign.center,
              style: const TextStyle(
                fontSize: 13,
                color: Color(0xFF94A3B8),
                height: 1.5,
              ),
            ),
            const SizedBox(height: 28),
            ElevatedButton.icon(
              onPressed: () => _reloadTab(index),
              icon: const Text('🔄'),
              label: const Text(
                'إعادة المحاولة الآن',
                style: TextStyle(fontWeight: FontWeight.bold),
              ),
              style: ElevatedButton.styleFrom(
                backgroundColor: const Color(0xFFF59E0B),
                foregroundColor: const Color(0xFF0B0F19),
                padding: const EdgeInsets.symmetric(
                  horizontal: 28,
                  vertical: 14,
                ),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(16),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: false,
      onPopInvoked: (didPop) async {
        if (didPop) return;
        final currentCtrl = _controllers[_currentIndex];
        if (currentCtrl != null && await currentCtrl.canGoBack()) {
          currentCtrl.goBack();
        } else if (_currentIndex != 0) {
          // العودة للتبويب الرئيسي (الرئيسية) قبل الخروج
          _switchToTab(0);
        } else {
          // تأكيد الخروج من التطبيق
          final shouldExit = await showDialog<bool>(
            context: context,
            builder: (ctx) => AlertDialog(
              backgroundColor: const Color(0xFF1E293B),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(20),
              ),
              title: const Text(
                'الخروج من تطبيق دورك؟',
                style: TextStyle(fontWeight: FontWeight.bold),
              ),
              content: const Text(
                'هل تريد إغلاق التطبيق ومغادرة المنصة؟',
                style: TextStyle(fontSize: 13, color: Color(0xFF94A3B8)),
              ),
              actions: [
                TextButton(
                  onPressed: () => Navigator.of(ctx).pop(false),
                  child: const Text('إلغاء', style: TextStyle(color: Colors.white)),
                ),
                ElevatedButton(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFFE11D48),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(12),
                    ),
                  ),
                  onPressed: () => Navigator.of(ctx).pop(true),
                  child: const Text('خروج', style: TextStyle(color: Colors.white)),
                ),
              ],
            ),
          );
          if (shouldExit ?? false) {
            SystemNavigator.pop();
          }
        }
      },
      child: Scaffold(
        backgroundColor: const Color(0xFF0B0F19),
        body: SafeArea(
          child: IndexedStack(
            index: _currentIndex,
            children: List.generate(4, (index) {
              if (!_isTabLoaded[index] || _controllers[index] == null) {
                return const SizedBox.shrink();
              }
              return Stack(
                children: [
                  // 1. The WebView Core with Native Pull-To-Refresh
                  if (!_hasErrorTab[index])
                    RefreshIndicator(
                      color: const Color(0xFFF59E0B),
                      backgroundColor: const Color(0xFF1E293B),
                      onRefresh: () async {
                        _reloadTab(index);
                      },
                      child: WebViewWidget(controller: _controllers[index]!),
                    ),

                  // 2. Native Loading Skeleton (Shown only until first page load completes, eliminating black void)
                  if (!_isPageFinished[index] && !_hasErrorTab[index])
                    Container(
                      color: const Color(0xFF0B0F19),
                      child: Center(
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Container(
                              width: 68,
                              height: 68,
                              decoration: BoxDecoration(
                                color: const Color(0xFF1E293B),
                                borderRadius: BorderRadius.circular(22),
                                border: Border.all(
                                  color: const Color(0xFFF59E0B).withOpacity(0.25),
                                ),
                              ),
                              child: Center(
                                child: Text(
                                  _tabIcons[index],
                                  style: const TextStyle(fontSize: 32),
                                ),
                              ),
                            ),
                            const SizedBox(height: 18),
                            const SizedBox(
                              width: 26,
                              height: 26,
                              child: CircularProgressIndicator(
                                strokeWidth: 2.5,
                                valueColor: AlwaysStoppedAnimation<Color>(Color(0xFFF59E0B)),
                              ),
                            ),
                            const SizedBox(height: 12),
                            Text(
                              _tabLoadingLabels[index],
                              style: const TextStyle(
                                color: Color(0xFF94A3B8),
                                fontSize: 13,
                                fontWeight: FontWeight.bold,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),

                  // 3. Custom Native Offline / Error View
                  if (_hasErrorTab[index])
                    _buildErrorView(index),
                ],
              );
            }),
          ),
        ),

        // 3. Native Bottom Navigation Bar (أصلي وسريع 0ms مع IndexedStack)
        bottomNavigationBar: Container(
          decoration: const BoxDecoration(
            color: Color(0xFF0F172A),
            border: Border(
              top: BorderSide(
                color: Color(0xFF1E293B),
                width: 1.0,
              ),
            ),
          ),
          child: BottomNavigationBar(
            currentIndex: _currentIndex,
            onTap: _onTabTapped,
            backgroundColor: const Color(0xFF0F172A),
            selectedItemColor: const Color(0xFFF59E0B),
            unselectedItemColor: const Color(0xFF94A3B8),
            selectedFontSize: 11,
            unselectedFontSize: 11,
            type: BottomNavigationBarType.fixed,
            elevation: 8,
            items: const [
              BottomNavigationBarItem(
                icon: Icon(Icons.home_rounded),
                activeIcon: Icon(Icons.home_rounded, color: Color(0xFFF59E0B)),
                label: 'الرئيسية',
              ),
              BottomNavigationBarItem(
                icon: Icon(Icons.content_cut_rounded),
                activeIcon: Icon(Icons.content_cut_rounded, color: Color(0xFFF59E0B)),
                label: 'الصالونات',
              ),
              BottomNavigationBarItem(
                icon: Icon(Icons.two_wheeler_rounded),
                activeIcon: Icon(Icons.two_wheeler_rounded, color: Color(0xFFF59E0B)),
                label: 'حلاق للدار',
              ),
              BottomNavigationBarItem(
                icon: Icon(Icons.person_rounded),
                activeIcon: Icon(Icons.person_rounded, color: Color(0xFFF59E0B)),
                label: 'حسابي',
              ),
            ],
          ),
        ),
      ),
    );
  }
}
