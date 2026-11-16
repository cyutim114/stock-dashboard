// Global variables
let currentStock = null;
let compareChart = null;

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    loadSectors();
    
    // Setup indicator description toggle
    setupIndicatorDescriptions();
});

// Tab switching
function switchTab(tabName) {
    console.log('switchTab called with:', tabName);
    
    // Hide all tab contents
    document.querySelectorAll('.tab-content').forEach(tab => {
        tab.classList.remove('active');
    });
    
    // Remove active class from all tab buttons
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('active');
    });
    
    // Show selected tab
    const tabElement = document.getElementById(tabName + '-tab');
    if (tabElement) {
        tabElement.classList.add('active');
        console.log('Activated tab:', tabName + '-tab');
    } else {
        console.error('Tab element not found:', tabName + '-tab');
    }
    
    // Update button - handle both click event and programmatic calls
    if (event && event.target) {
        const btn = event.target.closest('.tab-btn');
        if (btn) {
            btn.classList.add('active');
            console.log('Activated button via event');
        }
    } else {
        // Find and activate the corresponding button for programmatic calls
        const tabButtons = document.querySelectorAll('.tab-btn');
        let found = false;
        tabButtons.forEach((btn, index) => {
            const onclickAttr = btn.getAttribute('onclick');
            if (onclickAttr && onclickAttr.includes(`'${tabName}'`) || onclickAttr.includes(`"${tabName}"`)) {
                btn.classList.add('active');
                found = true;
                console.log('Activated button via attribute match');
            }
        });
        
        if (!found && tabName === 'search') {
            // Special case: if search tab, activate the first button
            tabButtons[0].classList.add('active');
            console.log('Activated first button (search tab)');
        }
    }
}

// Info tab switching
function switchInfoTab(tabName) {
    // Hide all info tab contents
    document.querySelectorAll('.info-tab-content').forEach(tab => {
        tab.classList.remove('active');
    });
    
    // Remove active class from all info tab buttons
    document.querySelectorAll('.info-tab-btn').forEach(btn => {
        btn.classList.remove('active');
    });
    
    // Show selected info tab
    document.getElementById(tabName + '-tab').classList.add('active');
    event.target.closest('.info-tab-btn').classList.add('active');
}

// Search stocks
function searchStocks() {
    const query = document.getElementById('search-input').value.trim();
    
    if (!query) {
        alert('請輸入搜尋關鍵詞');
        return;
    }
    
    const resultsContainer = document.getElementById('search-results');
    resultsContainer.innerHTML = '<div class="loading">搜尋中...<i class="fas fa-spinner fa-spin"></i></div>';
    
    fetch('/api/search', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ query: query })
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            resultsContainer.innerHTML = `<div class="empty-state"><p>❌ ${data.error}</p></div>`;
            return;
        }
        
        if (data.results.length === 0) {
            resultsContainer.innerHTML = '<div class="empty-state"><p>未找到相符的結果</p></div>';
            return;
        }
        
        resultsContainer.innerHTML = '';
        data.results.forEach(result => {
            const card = document.createElement('div');
            card.className = 'result-card';
            card.onclick = () => selectStock(result.symbol);
            card.innerHTML = `
                <div class="symbol">${result.symbol}</div>
                <div class="name">${result.longname || result.shortname || 'N/A'}</div>
                <div class="meta">
                    <div>${result.exchange || 'N/A'}</div>
                    <div>${result.quoteType || 'N/A'}</div>
                </div>
            `;
            resultsContainer.appendChild(card);
        });
    })
    .catch(error => {
        console.error('Search error:', error);
        resultsContainer.innerHTML = '<div class="empty-state"><p>❌ 搜尋失敗，請重試</p></div>';
    });
}

// Select stock from search results
function selectStock(symbol) {
    currentStock = symbol;
    document.getElementById('search-results').style.display = 'none';
    document.getElementById('stock-detail').style.display = 'block';
    
    // Load stock details
    loadStockDetails(symbol);
    
    // Load chart
    updateChart();
    
    // Load news
    loadNews(symbol);
    
    // Load technical indicators
    loadTechnicalIndicators(symbol);
}

// Load stock details
function loadStockDetails(symbol) {
    fetch(`/api/ticker/${symbol}`)
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            alert('❌ ' + data.error);
            return;
        }
        
        // Update header
        document.getElementById('stock-name').textContent = data.company.name;
        document.getElementById('stock-symbol').textContent = symbol;
        
        const priceDisplay = document.getElementById('stock-price-display');
        const currentPrice = data.price.current;
        const previousClose = data.price.previousClose;
        const change = currentPrice - previousClose;
        const changePercent = ((change / previousClose) * 100).toFixed(2);
        
        priceDisplay.innerHTML = `
            <div class="price" style="font-size: 2rem; margin-bottom: 5px;">$${currentPrice}</div>
            <div class="${change >= 0 ? 'price-up' : 'price-down'}" style="font-size: 1rem;">
                ${change >= 0 ? '↑' : '↓'} ${Math.abs(change).toFixed(2)} (${changePercent}%)
            </div>
        `;
        
        // Company Info
        const companyInfo = document.getElementById('company-info');
        companyInfo.innerHTML = `
            <div class="info-row">
                <span class="info-label">產業：</span>
                <span class="info-value">${data.company.sector}</span>
            </div>
            <div class="info-row">
                <span class="info-label">行業：</span>
                <span class="info-value">${data.company.industry}</span>
            </div>
            <div class="info-row">
                <span class="info-label">網站：</span>
                <span class="info-value"><a href="${data.company.website}" target="_blank">${data.company.website}</a></span>
            </div>
            <div class="info-row">
                <span class="info-label">簡介：</span>
                <span class="info-value">${data.company.description}</span>
            </div>
        `;
        
        // Trading Info
        const tradingInfo = document.getElementById('trading-info');
        tradingInfo.innerHTML = `
            <div class="info-row">
                <span class="info-label">開盤價：</span>
                <span class="info-value">$${data.price.open}</span>
            </div>
            <div class="info-row">
                <span class="info-label">今日高低：</span>
                <span class="info-value">$${data.price.dayHigh} ~ $${data.price.dayLow}</span>
            </div>
            <div class="info-row">
                <span class="info-label">52週高低：</span>
                <span class="info-value">$${data.price.fiftyTwoWeekHigh} ~ $${data.price.fiftyTwoWeekLow}</span>
            </div>
            <div class="info-row">
                <span class="info-label">成交量：</span>
                <span class="info-value">${formatNumber(data.trading.volume)}</span>
            </div>
            <div class="info-row">
                <span class="info-label">平均成交量：</span>
                <span class="info-value">${formatNumber(data.trading.averageVolume)}</span>
            </div>
            <div class="info-row">
                <span class="info-label">買入/賣出：</span>
                <span class="info-value">$${data.trading.bid} / $${data.trading.ask}</span>
            </div>
        `;
        
        // Valuation Info
        const valuationInfo = document.getElementById('valuation-info');
        valuationInfo.innerHTML = `
            <div class="info-row">
                <span class="info-label">市值：</span>
                <span class="info-value">${formatNumber(data.valuation.marketCap)}</span>
            </div>
            <div class="info-row">
                <span class="info-label">本益比 (P/E)：</span>
                <span class="info-value">${data.valuation.trailingPE}</span>
            </div>
            <div class="info-row">
                <span class="info-label">遠期P/E：</span>
                <span class="info-value">${data.valuation.forwardPE}</span>
            </div>
            <div class="info-row">
                <span class="info-label">市淨率 (P/B)：</span>
                <span class="info-value">${data.valuation.priceToBook}</span>
            </div>
            <div class="info-row">
                <span class="info-label">PEG比：</span>
                <span class="info-value">${data.valuation.pegRatio}</span>
            </div>
        `;
        
        // Dividend Info
        const dividendInfo = document.getElementById('dividend-info');
        dividendInfo.innerHTML = `
            <div class="info-row">
                <span class="info-label">股息年率：</span>
                <span class="info-value">${data.dividends.dividendRate || 'N/A'}</span>
            </div>
            <div class="info-row">
                <span class="info-label">股息收益率：</span>
                <span class="info-value">${data.dividends.dividendYield}%</span>
            </div>
            <div class="info-row">
                <span class="info-label">除息日期：</span>
                <span class="info-value">${data.dividends.exDividendDate}</span>
            </div>
            <div class="info-row">
                <span class="info-label">股息支付比：</span>
                <span class="info-value">${data.dividends.payoutRatio}%</span>
            </div>
        `;
        
        // Performance Info
        const performanceInfo = document.getElementById('performance-info');
        performanceInfo.innerHTML = `
            <div class="info-row">
                <span class="info-label">β係數：</span>
                <span class="info-value">${data.performance.beta}</span>
            </div>
            <div class="info-row">
                <span class="info-label">50日均線：</span>
                <span class="info-value">$${data.performance.fiftyDayAverage}</span>
            </div>
            <div class="info-row">
                <span class="info-label">200日均線：</span>
                <span class="info-value">$${data.performance.twoHundredDayAverage}</span>
            </div>
        `;
    })
    .catch(error => {
        console.error('Load stock details error:', error);
        alert('❌ 載入股票資訊失敗');
    });
}

// Update chart
function updateChart() {
    if (!currentStock) return;
    
    const period = document.getElementById('chart-period').value;
    const chartLoading = document.getElementById('chart-loading');
    const chartImg = document.getElementById('stock-chart');
    
    chartLoading.style.display = 'flex';
    chartImg.style.display = 'none';
    
    fetch(`/api/ticker/${currentStock}/chart?period=${period}`)
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            chartLoading.innerHTML = `<div class="empty-state"><p>❌ ${data.error}</p></div>`;
            return;
        }
        
        chartImg.src = data.chart;
        chartLoading.style.display = 'none';
        chartImg.style.display = 'block';
    })
    .catch(error => {
        console.error('Chart update error:', error);
        chartLoading.innerHTML = '<div class="empty-state"><p>❌ 載入圖表失敗</p></div>';
    });
}

// Load news
function loadNews(symbol) {
    fetch(`/api/ticker/${symbol}/news`)
    .then(response => response.json())
    .then(data => {
        const newsList = document.getElementById('news-list');
        
        if (data.error || !data.news || data.news.length === 0) {
            newsList.innerHTML = '<div class="empty-state"><p>暫無新聞資訊</p></div>';
            return;
        }
        
        newsList.innerHTML = '';
        data.news.forEach(news => {
            const newsItem = document.createElement('div');
            newsItem.className = 'news-item';
            const thumbnail = news.thumbnail ? `<img src="${news.thumbnail}" class="news-thumbnail">` : '';
            newsItem.innerHTML = `
                ${thumbnail}
                <div class="news-content">
                    <div class="news-title">${news.title}</div>
                    <div class="news-publisher">來源：${news.publisher}</div>
                    <a href="${news.link}" target="_blank" class="news-link">閱讀全文 →</a>
                </div>
            `;
            newsList.appendChild(newsItem);
        });
    })
    .catch(error => {
        console.error('Load news error:', error);
    });
}

// Back to search
function backToSearch() {
    document.getElementById('search-results').style.display = 'block';
    document.getElementById('stock-detail').style.display = 'none';
    currentStock = null;
}

// Compare stocks
function compareStocks() {
    const symbolsInput = document.getElementById('compare-symbols').value.trim();
    const period = document.getElementById('compare-period').value;
    
    if (!symbolsInput) {
        alert('請輸入股票代碼');
        return;
    }
    
    const symbols = symbolsInput.split(',').map(s => s.trim().toUpperCase()).filter(s => s);
    
    if (symbols.length > 5) {
        alert('最多只能比較5個股票');
        return;
    }
    
    const compareResults = document.getElementById('compare-results');
    compareResults.style.display = 'block';
    const compareLoading = document.getElementById('compare-loading');
    compareLoading.style.display = 'flex';
    
    const compareChartCanvas = document.getElementById('compare-chart');
    const compareTable = document.getElementById('compare-table');
    compareChartCanvas.style.display = 'none';
    compareTable.style.display = 'none';
    
    fetch('/api/compare', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ symbols: symbols, period: period })
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            compareLoading.innerHTML = `<p>❌ ${data.error}</p>`;
            return;
        }
        
        // Prepare chart data
        const comparison = data.comparison;
        const labels = comparison[Object.keys(comparison)[0]].dates;
        
        // Chart colors
        const colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd'];
        
        const datasets = Object.keys(comparison).map((symbol, index) => {
            // Normalize prices to percentage change
            const prices = comparison[symbol].prices;
            const initialPrice = prices[0];
            const normalizedPrices = prices.map(p => ((p - initialPrice) / initialPrice) * 100);
            
            return {
                label: symbol,
                data: normalizedPrices,
                borderColor: colors[index % colors.length],
                backgroundColor: colors[index % colors.length] + '20',
                tension: 0.1,
                fill: false,
                pointRadius: 0
            };
        });
        
        // Destroy existing chart if it exists
        if (compareChart) {
            compareChart.destroy();
        }
        
        // Create new chart
        const ctx = compareChartCanvas.getContext('2d');
        compareChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: datasets
            },
            options: {
                responsive: true,
                plugins: {
                    legend: {
                        display: true,
                        position: 'top'
                    },
                    title: {
                        display: true,
                        text: `股票對比分析 (${period}) - 價格變化百分比`
                    }
                },
                scales: {
                    y: {
                        title: {
                            display: true,
                            text: '價格變化 (%)'
                        }
                    }
                }
            }
        });
        
        // Create comparison table
        let tableHTML = '<table class="data-table"><thead><tr><th>股票</th><th>初始價格</th><th>最終價格</th><th>收益率</th></tr></thead><tbody>';
        
        for (const symbol in comparison) {
            const data = comparison[symbol];
            const returnClass = data.returns >= 0 ? 'price-up' : 'price-down';
            tableHTML += `
                <tr>
                    <td><strong>${symbol}</strong></td>
                    <td>$${data.initial_price}</td>
                    <td>$${data.final_price}</td>
                    <td class="${returnClass}">${data.returns >= 0 ? '+' : ''}${data.returns}%</td>
                </tr>
            `;
        }
        
        tableHTML += '</tbody></table>';
        compareTable.innerHTML = tableHTML;
        
        compareLoading.style.display = 'none';
        compareChartCanvas.style.display = 'block';
        compareTable.style.display = 'block';
    })
    .catch(error => {
        console.error('Compare error:', error);
        compareLoading.innerHTML = '<p>❌ 比較失敗，請重試</p>';
    });
}

// Load sectors
function loadSectors() {
    fetch('/api/sectors')
    .then(response => response.json())
    .then(data => {
        const sectorSelect = document.getElementById('sector-select');
        sectorSelect.innerHTML = '<option value="">選擇行業</option>';
        
        data.sectors.forEach(sector => {
            const option = document.createElement('option');
            option.value = sector;
            option.textContent = sector;
            sectorSelect.appendChild(option);
        });
    })
    .catch(error => {
        console.error('Load sectors error:', error);
    });
}

// Global variable to store sector results
let currentSectorResults = [];
let currentSortMethod = 'price';

// Load sector top companies
function loadSectorTop() {
    const sector = document.getElementById('sector-select').value;
    const count = parseInt(document.getElementById('sector-count').value) || 20;
    
    if (!sector) {
        alert('請選擇行業');
        return;
    }
    
    const sectorResults = document.getElementById('sector-results');
    sectorResults.style.display = 'block';
    const sectorLoading = document.getElementById('sector-loading');
    const sectorTable = document.getElementById('sector-table');
    const sortBar = document.getElementById('sector-sort-bar');
    
    sectorLoading.style.display = 'flex';
    sectorTable.style.display = 'none';
    sortBar.style.display = 'none';
    
    fetch(`/api/sector/${encodeURIComponent(sector)}/top?count=${count}&sort=${currentSortMethod}`)
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            sectorLoading.innerHTML = `<p style="color: #d62728;">❌ ${data.error}</p>`;
            return;
        }
        
        // Store results for sorting
        currentSectorResults = data.companies || [];
        
        // Display the table with current sort
        displaySectorResults(currentSectorResults);
        
        sectorLoading.style.display = 'none';
        sortBar.style.display = 'flex';
        sectorTable.style.display = 'table';
    })
    .catch(error => {
        console.error('Load sector top error:', error);
        sectorLoading.innerHTML = '<p style="color: #d62728;">❌ 載入失敗，請重試</p>';
    });
}

// Sort sector results
function sortSectorResults(sortMethod) {
    currentSortMethod = sortMethod;
    
    // Update button states
    document.querySelectorAll('.sort-btn').forEach(btn => {
        btn.classList.remove('active');
    });
    event.target.closest('.sort-btn').classList.add('active');
    
    // Sort the data
    let sortedData = [...currentSectorResults];
    
    if (sortMethod === 'change') {
        sortedData.sort((a, b) => (b.changePercent || 0) - (a.changePercent || 0));
    } else if (sortMethod === 'volume') {
        sortedData.sort((a, b) => (b.volume || 0) - (a.volume || 0));
    } else { // price
        sortedData.sort((a, b) => (b.regularMarketPrice || 0) - (a.regularMarketPrice || 0));
    }
    
    // Display sorted results
    displaySectorResults(sortedData);
}

// Display sector results in table
function displaySectorResults(companies) {
    const tbody = document.getElementById('sector-table-body');
    tbody.innerHTML = '';
    
    companies.forEach((company, index) => {
        const row = document.createElement('tr');
        const changePercent = company.changePercent || 0;
        const changeColor = changePercent > 0 ? '#2ca02c' : (changePercent < 0 ? '#d62728' : '#666');
        const changeSymbol = changePercent > 0 ? '↑' : (changePercent < 0 ? '↓' : '→');
        
        row.innerHTML = `
            <td>${index + 1}</td>
            <td>
                <span class="sector-symbol-link" style="cursor: pointer; color: var(--primary-color); font-weight: bold; text-decoration: underline; text-decoration-thickness: 2px;" 
                      data-symbol="${company.symbol}">${company.symbol}</span>
            </td>
            <td class="sector-company-link" style="cursor: pointer; color: var(--text-dark);"
                data-symbol="${company.symbol}">
                ${company.shortName || company.longName || 'N/A'}
            </td>
            <td style="text-align: right; font-weight: 500;">$${(company.regularMarketPrice || 0).toFixed(2)}</td>
            <td style="text-align: right; color: ${changeColor}; font-weight: 500;">
                ${changeSymbol} ${Math.abs(changePercent).toFixed(2)}%
            </td>
            <td style="text-align: right; font-size: 0.9rem; color: #666;">
                ${formatNumber(company.volume || 0)}
            </td>
            <td style="text-align: right; font-size: 0.9rem; color: #666;">
                ${formatNumber(company.marketCap || 0)}
            </td>
        `;
        tbody.appendChild(row);
    });
    
    // Add event listeners for symbol and company name clicks
    document.querySelectorAll('.sector-symbol-link, .sector-company-link').forEach(el => {
        el.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            const symbol = this.getAttribute('data-symbol');
            console.log('Clicking on symbol:', symbol); // 調試日誌
            goToStockSearch(symbol);
        });
    });
}

// Navigate to stock search and auto-search the symbol
function goToStockSearch(symbol) {
    console.log('goToStockSearch called with symbol:', symbol);
    
    if (!symbol) {
        console.error('No symbol provided to goToStockSearch');
        return;
    }
    
    try {
        // Switch to search tab
        console.log('Switching to search tab...');
        switchTab('search');
        
        // Set the search input value
        const searchInput = document.getElementById('search-input');
        if (searchInput) {
            searchInput.value = symbol;
            console.log('Search input set to:', symbol);
        } else {
            console.error('Search input element not found');
        }
        
        // Directly select the stock (similar to clicking from search results)
        console.log('Calling selectStock for:', symbol);
        setTimeout(() => {
            selectStock(symbol);
        }, 300);
    } catch (error) {
        console.error('Error in goToStockSearch:', error);
    }
}

// Load technical indicators
function loadTechnicalIndicators(symbol) {
    if (!symbol) return;
    
    const period = document.getElementById('chart-period').value;
    const indicatorsLoading = document.getElementById('indicators-loading');
    const priceIndicators = document.getElementById('price-indicators');
    const volumeIndicators = document.getElementById('volume-indicators');
    const combinedIndicators = document.getElementById('combined-indicators');
    
    indicatorsLoading.style.display = 'flex';
    priceIndicators.style.display = 'none';
    volumeIndicators.style.display = 'none';
    combinedIndicators.style.display = 'none';
    
    fetch(`/api/ticker/${symbol}/indicators?period=${period}`)
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            indicatorsLoading.innerHTML = `<p>❌ ${data.error}</p>`;
            return;
        }
        
        const indicators = data.indicators;
        
        // Display Price-Type Indicators
        if (indicators['📈 價格型指標']) {
            const priceInd = indicators['📈 價格型指標'];
            
            // MA
            if (priceInd['移動平均線 (MA)']) {
                const ma = priceInd['移動平均線 (MA)'];
                const lastIdx = ma.MA5 ? ma.MA5.length - 1 : -1;
                if (lastIdx >= 0) {
                    const ma5 = ma.MA5[lastIdx];
                    const ma20 = ma.MA20[lastIdx];
                    const ma50 = ma.MA50[lastIdx];
                    
                    let maTrend = '平穩';
                    let trendColor = '#666';
                    
                    if (ma5 > ma20 && ma20 > ma50) {
                        maTrend = '強勢上升 📈';
                        trendColor = '#2ca02c';
                    } else if (ma5 > ma20) {
                        maTrend = '短期上升';
                        trendColor = '#2ca02c';
                    } else if (ma5 < ma20 && ma20 < ma50) {
                        maTrend = '強勢下降 📉';
                        trendColor = '#d62728';
                    } else if (ma5 < ma20) {
                        maTrend = '短期下降';
                        trendColor = '#d62728';
                    }
                    
                    document.getElementById('ma-values').innerHTML = `
                        <div class="indicator-value">MA5: $${ma5?.toFixed(2) || 'N/A'}</div>
                        <div class="indicator-value">MA10: $${ma.MA10[lastIdx]?.toFixed(2) || 'N/A'}</div>
                        <div class="indicator-value">MA20: $${ma20?.toFixed(2) || 'N/A'}</div>
                        <div class="indicator-value">MA50: $${ma50?.toFixed(2) || 'N/A'}</div>
                        <div class="indicator-value" style="color: ${trendColor}; font-weight: 600;">${maTrend}</div>
                    `;
                }
            }
            
            // MACD
            if (priceInd['MACD']) {
                const macd = priceInd['MACD'];
                const lastIdx = macd.macd ? macd.macd.length - 1 : -1;
                if (lastIdx >= 0) {
                    const macdVal = macd.macd[lastIdx];
                    const signalVal = macd.signal[lastIdx];
                    const histVal = macd.histogram[lastIdx];
                    
                    let macdSignal = '無交叉';
                    let signalColor = '#666';
                    
                    if (macdVal > signalVal && histVal > 0) {
                        macdSignal = '金叉（買入信號）✅';
                        signalColor = '#2ca02c';
                    } else if (macdVal < signalVal && histVal < 0) {
                        macdSignal = '死叉（賣出信號）⚠️';
                        signalColor = '#d62728';
                    } else if (macdVal > signalVal) {
                        macdSignal = '上升趨勢';
                        signalColor = '#2ca02c';
                    } else if (macdVal < signalVal) {
                        macdSignal = '下降趨勢';
                        signalColor = '#d62728';
                    }
                    
                    document.getElementById('macd-values').innerHTML = `
                        <div class="indicator-value">MACD: ${macdVal?.toFixed(4) || 'N/A'}</div>
                        <div class="indicator-value">Signal: ${signalVal?.toFixed(4) || 'N/A'}</div>
                        <div class="indicator-value">Histogram: ${histVal?.toFixed(4) || 'N/A'}</div>
                        <div class="indicator-value" style="color: ${signalColor}; font-weight: 600;">${macdSignal}</div>
                    `;
                }
            }
            
            // RSI
            if (priceInd['RSI']) {
                const rsi = priceInd['RSI'];
                const lastIdx = rsi.rsi ? rsi.rsi.length - 1 : -1;
                if (lastIdx >= 0) {
                    const rsiValue = rsi.rsi[lastIdx];
                    let rsiSignal = '中立';
                    let signalColor = '#2ca02c';
                    if (rsiValue > 70) {
                        rsiSignal = '超買 ⚠️';
                        signalColor = '#d62728';
                    } else if (rsiValue < 30) {
                        rsiSignal = '超賣 ⚠️';
                        signalColor = '#ff9800';
                    }
                    document.getElementById('rsi-values').innerHTML = `
                        <div class="indicator-value">RSI: ${rsiValue?.toFixed(2) || 'N/A'}</div>
                        <div class="indicator-value" style="color: ${signalColor}; font-weight: 600;">${rsiSignal}</div>
                    `;
                }
            }
            
            // KD
            if (priceInd['KD']) {
                const kd = priceInd['KD'];
                const lastIdx = kd.k ? kd.k.length - 1 : -1;
                if (lastIdx >= 0) {
                    const kVal = kd.k[lastIdx];
                    const dVal = kd.d[lastIdx];
                    
                    let kdSignal = '平穩';
                    let signalColor = '#666';
                    
                    if (kVal > dVal && kVal > 80) {
                        kdSignal = '金叉（買入信號）✅';
                        signalColor = '#2ca02c';
                    } else if (kVal < dVal && kVal < 20) {
                        kdSignal = '死叉（賣出信號）⚠️';
                        signalColor = '#d62728';
                    } else if (kVal > 80 || dVal > 80) {
                        kdSignal = '超買區域 ⚠️';
                        signalColor = '#d62728';
                    } else if (kVal < 20 || dVal < 20) {
                        kdSignal = '超賣區域（買入）';
                        signalColor = '#ff9800';
                    }
                    
                    document.getElementById('kd-values').innerHTML = `
                        <div class="indicator-value">K 值: ${kVal?.toFixed(2) || 'N/A'}</div>
                        <div class="indicator-value">D 值: ${dVal?.toFixed(2) || 'N/A'}</div>
                        <div class="indicator-value" style="color: ${signalColor}; font-weight: 600;">${kdSignal}</div>
                    `;
                }
            }
            
            // Bollinger Bands
            if (priceInd['布林通道 (Bollinger Bands)']) {
                const bb = priceInd['布林通道 (Bollinger Bands)'];
                const lastIdx = bb.middle ? bb.middle.length - 1 : -1;
                if (lastIdx >= 0) {
                    const upper = bb.upper[lastIdx];
                    const middle = bb.middle[lastIdx];
                    const lower = bb.lower[lastIdx];
                    const currentPrice = middle; // Approximate price
                    
                    let bbSignal = '正常';
                    let signalColor = '#666';
                    
                    if (currentPrice > upper * 0.95) {
                        bbSignal = '觸及上軌（超買）⚠️';
                        signalColor = '#d62728';
                    } else if (currentPrice < lower * 1.05) {
                        bbSignal = '觸及下軌（超賣）';
                        signalColor = '#ff9800';
                    } else if (currentPrice > middle) {
                        bbSignal = '上半區（看強）';
                        signalColor = '#2ca02c';
                    } else if (currentPrice < middle) {
                        bbSignal = '下半區（看弱）';
                        signalColor = '#d62728';
                    }
                    
                    document.getElementById('bb-values').innerHTML = `
                        <div class="indicator-value">上軌: $${upper?.toFixed(2) || 'N/A'}</div>
                        <div class="indicator-value">中軌: $${middle?.toFixed(2) || 'N/A'}</div>
                        <div class="indicator-value">下軌: $${lower?.toFixed(2) || 'N/A'}</div>
                        <div class="indicator-value" style="color: ${signalColor}; font-weight: 600;">${bbSignal}</div>
                    `;
                }
            }
            
            // BIAS
            if (priceInd['BIAS']) {
                const bias = priceInd['BIAS'];
                const lastIdx = bias.bias ? bias.bias.length - 1 : -1;
                if (lastIdx >= 0) {
                    const biasVal = bias.bias[lastIdx];
                    let biasSignal = '合理範圍';
                    let signalColor = '#666';
                    
                    if (biasVal > 5) {
                        biasSignal = '高估（回調風險）⚠️';
                        signalColor = '#d62728';
                    } else if (biasVal > 3) {
                        biasSignal = '偏高（謹慎)';
                        signalColor = '#ff9800';
                    } else if (biasVal < -5) {
                        biasSignal = '低估（反彈機會）';
                        signalColor = '#2ca02c';
                    } else if (biasVal < -3) {
                        biasSignal = '偏低（關注)';
                        signalColor = '#1e90ff';
                    }
                    
                    document.getElementById('bias-values').innerHTML = `
                        <div class="indicator-value">乖離率: ${biasVal?.toFixed(3) || 'N/A'}%</div>
                        <div class="indicator-value" style="color: ${signalColor}; font-weight: 600;">${biasSignal}</div>
                    `;
                }
            }
            
            // ARBR
            if (priceInd['ARBR']) {
                const arbr = priceInd['ARBR'];
                const lastIdx = arbr.ar ? arbr.ar.length - 1 : -1;
                if (lastIdx >= 0) {
                    const arVal = arbr.ar[lastIdx];
                    const brVal = arbr.br[lastIdx];
                    
                    let arbrSignal = '平衡';
                    let signalColor = '#666';
                    
                    if (arVal > brVal && arVal > 100) {
                        arbrSignal = '買方強勢 📈';
                        signalColor = '#2ca02c';
                    } else if (brVal > arVal && brVal > 100) {
                        arbrSignal = '賣方強勢 📉';
                        signalColor = '#d62728';
                    } else if (arVal > brVal) {
                        arbrSignal = '買方略占上風';
                        signalColor = '#2ca02c';
                    } else if (brVal > arVal) {
                        arbrSignal = '賣方略占上風';
                        signalColor = '#d62728';
                    }
                    
                    document.getElementById('arbr-values').innerHTML = `
                        <div class="indicator-value">AR: ${arVal?.toFixed(2) || 'N/A'}</div>
                        <div class="indicator-value">BR: ${brVal?.toFixed(2) || 'N/A'}</div>
                        <div class="indicator-value" style="color: ${signalColor}; font-weight: 600;">${arbrSignal}</div>
                    `;
                }
            }
            
            priceIndicators.style.display = 'block';
        }
        
        // Display Volume-Type Indicators
        if (indicators['📊 量能型指標']) {
            const volInd = indicators['📊 量能型指標'];
            
            // VMA
            if (volInd['成交量均線 (VMA)']) {
                const vma = volInd['成交量均線 (VMA)'];
                const lastIdx = vma.vma5 ? vma.vma5.length - 1 : -1;
                if (lastIdx >= 0) {
                    const vma5 = vma.vma5[lastIdx];
                    const vma20 = vma.vma20[lastIdx];
                    
                    let vmaSignal = '成交量平穩';
                    let signalColor = '#666';
                    
                    if (vma5 > vma20) {
                        vmaSignal = '成交量上升 📈';
                        signalColor = '#2ca02c';
                    } else if (vma5 < vma20) {
                        vmaSignal = '成交量下降 📉';
                        signalColor = '#d62728';
                    }
                    
                    document.getElementById('vma-values').innerHTML = `
                        <div class="indicator-value">VMA5: ${formatNumber(vma5)}</div>
                        <div class="indicator-value">VMA20: ${formatNumber(vma20)}</div>
                        <div class="indicator-value" style="color: ${signalColor}; font-weight: 600;">${vmaSignal}</div>
                    `;
                }
            }
            
            // Volume Ratio
            if (volInd['量比']) {
                const vr = volInd['量比'];
                const lastIdx = vr.volume_ratio ? vr.volume_ratio.length - 1 : -1;
                if (lastIdx >= 0) {
                    const vrVal = vr.volume_ratio[lastIdx];
                    let vrSignal = '成交量平均';
                    let signalColor = '#666';
                    
                    if (vrVal > 2) {
                        vrSignal = '異常高量🔥';
                        signalColor = '#d62728';
                    } else if (vrVal > 1.5) {
                        vrSignal = '量能增加';
                        signalColor = '#2ca02c';
                    } else if (vrVal > 1) {
                        vrSignal = '量能略增';
                        signalColor = '#2ca02c';
                    } else if (vrVal > 0.8) {
                        vrSignal = '量能萎縮';
                        signalColor = '#ff9800';
                    } else {
                        vrSignal = '成交量不足 ⚠️';
                        signalColor = '#d62728';
                    }
                    
                    document.getElementById('vr-values').innerHTML = `
                        <div class="indicator-value">量比: ${vrVal?.toFixed(2) || 'N/A'}</div>
                        <div class="indicator-value" style="color: ${signalColor}; font-weight: 600;">${vrSignal}</div>
                    `;
                }
            }
            
            volumeIndicators.style.display = 'block';
        }
        
        // Display Price-Volume Combined Indicators
        if (indicators['🔗 價量結合型指標']) {
            const comInd = indicators['🔗 價量結合型指標'];
            
            // OBV
            if (comInd['OBV']) {
                const obv = comInd['OBV'];
                const lastIdx = obv.obv ? obv.obv.length - 1 : -1;
                if (lastIdx >= 0) {
                    const obvVal = obv.obv[lastIdx];
                    const prevVal = lastIdx > 0 ? obv.obv[lastIdx - 1] : obvVal;
                    
                    let obvSignal = '平穩';
                    let signalColor = '#666';
                    
                    if (obvVal > prevVal) {
                        obvSignal = '成交量支持上升 📈';
                        signalColor = '#2ca02c';
                    } else if (obvVal < prevVal) {
                        obvSignal = '成交量支持下降 📉';
                        signalColor = '#d62728';
                    }
                    
                    document.getElementById('obv-values').innerHTML = `
                        <div class="indicator-value">OBV: ${formatNumber(obvVal)}</div>
                        <div class="indicator-value" style="color: ${signalColor}; font-weight: 600;">${obvSignal}</div>
                    `;
                }
            }
            
            // VWAP
            if (comInd['VWAP']) {
                const vwap = comInd['VWAP'];
                const lastIdx = vwap.vwap ? vwap.vwap.length - 1 : -1;
                if (lastIdx >= 0) {
                    const vwapVal = vwap.vwap[lastIdx];
                    // Note: We're using VWAP as reference, in real scenario should compare with current price
                    // For now, we'll just show the VWAP value
                    
                    document.getElementById('vwap-values').innerHTML = `
                        <div class="indicator-value">VWAP: $${vwapVal?.toFixed(2) || 'N/A'}</div>
                        <div class="indicator-value" style="color: #1e90ff; font-weight: 600;">機構參考價格</div>
                    `;
                }
            }
            
            // Price-Volume Divergence
            if (comInd['量價背離']) {
                const pvd = comInd['量價背離'];
                const lastIdx = pvd.divergence ? pvd.divergence.length - 1 : -1;
                if (lastIdx >= 0) {
                    let pvdSignal = '無背離';
                    let pvdColor = '#6c757d';
                    if (pvd.divergence[lastIdx] > 0) {
                        pvdSignal = '正背離（買入信號）✅';
                        pvdColor = '#2ca02c';
                    } else if (pvd.divergence[lastIdx] < 0) {
                        pvdSignal = '負背離（賣出信號）⚠️';
                        pvdColor = '#d62728';
                    }
                    document.getElementById('pvd-values').innerHTML = `
                        <div class="indicator-value" style="color: ${pvdColor}; font-weight: 600;">${pvdSignal}</div>
                    `;
                }
            }
            
            combinedIndicators.style.display = 'block';
        }
        
        indicatorsLoading.style.display = 'none';
    })
    .catch(error => {
        console.error('Technical indicators error:', error);
        indicatorsLoading.innerHTML = '<p>❌ 載入技術指標失敗</p>';
    });
}

// Utility function to format numbers
function formatNumber(num) {
    if (!num) return 'N/A';
    
    if (num >= 1e9) {
        return (num / 1e9).toFixed(2) + 'B';
    } else if (num >= 1e6) {
        return (num / 1e6).toFixed(2) + 'M';
    } else if (num >= 1e3) {
        return (num / 1e3).toFixed(2) + 'K';
    } else {
        return num.toFixed(2);
    }
}

// Allow Enter key to search
document.addEventListener('keypress', function(event) {
    if (event.key === 'Enter') {
        if (document.activeElement === document.getElementById('search-input')) {
            searchStocks();
        } else if (document.activeElement === document.getElementById('compare-symbols')) {
            compareStocks();
        }
    }
});

// Setup indicator description toggle functionality
function setupIndicatorDescriptions() {
    document.addEventListener('click', function(event) {
        // Check if clicked element is an info icon
        if (event.target.classList.contains('info-icon')) {
            const infoIcon = event.target;
            const card = infoIcon.closest('.indicator-card');
            const description = card.querySelector('.indicator-description');
            
            if (description) {
                // Toggle display
                const isVisible = description.style.display !== 'none';
                description.style.display = isVisible ? 'none' : 'block';
                
                // Visual feedback on icon
                infoIcon.style.opacity = isVisible ? '0.6' : '1';
            }
        }
    });
    
    // Also show description on hover for better UX
    document.addEventListener('mouseenter', function(event) {
        if (event.target.classList.contains('info-icon')) {
            const card = event.target.closest('.indicator-card');
            const description = card.querySelector('.indicator-description');
            if (description && description.style.display === 'none') {
                event.target.style.opacity = '0.8';
            }
        }
    }, true);
    
    document.addEventListener('mouseleave', function(event) {
        if (event.target.classList.contains('info-icon')) {
            const card = event.target.closest('.indicator-card');
            const description = card.querySelector('.indicator-description');
            if (description && description.style.display === 'none') {
                event.target.style.opacity = '0.6';
            }
        }
    }, true);
}
